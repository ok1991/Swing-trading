import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swing_trading.cli import main_cli
from swing_trading.execution_plan import build_execution_plan
from swing_trading.quotes import QuoteSnapshot
from swing_trading.rotation_source import RotationSnapshot
from swing_trading.state import StateManager
from test_v4_authority import rotation


class CliSafetyTests(unittest.TestCase):
    def test_no_realtime_explicit_publish_is_rejected(self):
        with patch.object(
            sys,
            "argv",
            ["swing-trading", "--no-realtime", "--publish"],
        ):
            with self.assertRaises(SystemExit):
                main_cli()

    def test_live_order_run_persists_immutable_execution_plan_before_state(self):
        payload = rotation()
        snapshot = RotationSnapshot(
            payload=payload,
            source="local",
            source_url="local-test",
            fetched_at="2026-07-20 09:35:00",
            allow_rebalance=True,
        )
        quote_snapshot = Mock()
        quote_snapshot.prices = {code: 10.0 for code in payload["target_weights"]}
        quote_snapshot.prices["510300"] = 4.0
        quote_snapshot.diagnostics.return_value = {
            "mode": "SINA_REALTIME",
            "tradeable": True,
            "errors": [],
        }
        quote_snapshot.valuation_diagnostics.return_value = {
            "mode": "DAILY_MARK_TO_MARKET",
            "tradeable": True,
            "errors": [],
        }
        state = StateManager.initial()
        with (
            patch.object(sys, "argv", ["swing-trading", "--run-date", payload["execution_date"]]),
            patch.dict(os.environ, {"AUTO_GIT_PUSH": "false"}),
            patch("swing_trading.cli.ensure_directories"),
            patch("swing_trading.cli._configure_logging"),
            patch("swing_trading.cli.StateManager.load", return_value=state),
            patch("swing_trading.cli.StateManager.save") as state_save,
            patch("swing_trading.cli.RotationSource.load", return_value=snapshot),
            patch("swing_trading.cli.RealtimeQuote.fetch", return_value=quote_snapshot),
            patch("swing_trading.cli.TradeHTMLReporter.generate"),
            patch("swing_trading.cli.save_execution_feedback"),
            patch("swing_trading.cli.save_live_performance") as performance_save,
            patch("swing_trading.cli.save_execution_plan") as plan_save,
        ):
            main_cli()
        state_save.assert_called_once()
        plan_save.assert_called_once()
        saved_plan = plan_save.call_args.args[0]
        self.assertEqual(2, saved_plan["schema_version"])
        self.assertEqual(8, saved_plan["pre_trade_state"]["schema_version"])
        self.assertTrue(saved_plan["pre_trade_state_sha256"])
        self.assertGreater(saved_plan["order_count"], 0)
        self.assertEqual(
            payload["model_version"],
            performance_save.call_args.args[0]["model_version"],
        )
        self.assertEqual(
            saved_plan["plan_id"],
            saved_plan["orders"]["decision_diagnostics"]["plan_id"],
        )
        self.assertEqual(saved_plan["plan_id"], state["pending_broker_confirmation_plan_id"])

    def test_virtual_confirm_promotes_live_order_to_broker_confirmed(self):
        payload = rotation()
        snapshot = RotationSnapshot(
            payload=payload,
            source="local",
            source_url="local-test",
            fetched_at="2026-07-20 09:35:00",
            allow_rebalance=True,
        )
        quote_snapshot = Mock()
        quote_snapshot.prices = {code: 10.0 for code in payload["target_weights"]}
        quote_snapshot.prices["510300"] = 4.0
        quote_snapshot.diagnostics.return_value = {
            "mode": "SINA_REALTIME",
            "tradeable": True,
            "errors": [],
        }
        quote_snapshot.valuation_diagnostics.return_value = {
            "mode": "DAILY_MARK_TO_MARKET",
            "tradeable": True,
            "errors": [],
        }
        state = StateManager.initial()
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "public"
            feedback_path = public / "execution_feedback_latest.json"
            virtual_fills_path = public / "virtual_broker_fills_latest.json"
            with (
                patch.object(sys, "argv", ["swing-trading", "--run-date", payload["execution_date"], "--virtual-confirm"]),
                patch.dict(os.environ, {"AUTO_GIT_PUSH": "false"}),
                patch("swing_trading.cli.ensure_directories"),
                patch("swing_trading.cli._configure_logging"),
                patch("swing_trading.cli.StateManager.load", return_value=state),
                patch("swing_trading.cli.StateManager.save") as state_save,
                patch("swing_trading.cli.RotationSource.load", return_value=snapshot),
                patch("swing_trading.cli.RealtimeQuote.fetch", return_value=quote_snapshot),
                patch("swing_trading.cli.TradeHTMLReporter.generate"),
                patch("swing_trading.cli.execution_feedback_path", return_value=feedback_path),
                patch("swing_trading.cli.execution_feedback_virtual_path", return_value=virtual_fills_path),
                patch("swing_trading.cli.execution_feedback_history_path", return_value=public / "history.json"),
                patch("swing_trading.cli.save_live_performance"),
                patch("swing_trading.cli.save_execution_plan") as plan_save,
            ):
                main_cli()
            feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
            virtual_fills = json.loads(virtual_fills_path.read_text(encoding="utf-8"))
        plan_id = plan_save.call_args.args[0]["plan_id"]
        self.assertEqual("BROKER_CONFIRMED", feedback["evidence_level"])
        self.assertTrue(feedback["state_reconciliation_applied"])
        self.assertEqual("virtual-paper", virtual_fills["broker"])
        self.assertEqual(plan_id, virtual_fills["plan_id"])
        self.assertEqual("", state["pending_broker_confirmation_plan_id"])
        self.assertEqual(plan_id, state["last_execution_satisfied_plan_id"])
        state_save.assert_called_once()

    def test_invalid_pretrade_audit_blocks_production_source_orders(self):
        payload = rotation()
        snapshot = RotationSnapshot(
            payload=payload,
            source="local_sibling",
            source_url="local-production-test",
            fetched_at="2026-07-20 09:35:00",
            allow_rebalance=True,
        )
        quote_snapshot = Mock()
        quote_snapshot.prices = {code: 10.0 for code in payload["target_weights"]}
        quote_snapshot.prices["510300"] = 4.0
        quote_snapshot.diagnostics.return_value = {
            "mode": "SINA_REALTIME",
            "tradeable": True,
            "errors": [],
        }
        quote_snapshot.valuation_diagnostics.return_value = {
            "mode": "DAILY_MARK_TO_MARKET",
            "tradeable": True,
            "errors": [],
        }
        state = StateManager.initial()
        with (
            patch.object(sys, "argv", ["swing-trading", "--run-date", payload["execution_date"]]),
            patch.dict(os.environ, {"AUTO_GIT_PUSH": "false"}),
            patch("swing_trading.cli.ensure_directories"),
            patch("swing_trading.cli._configure_logging"),
            patch("swing_trading.cli.StateManager.load", return_value=state),
            patch("swing_trading.cli.StateManager.save"),
            patch("swing_trading.cli.RotationSource.load", return_value=snapshot),
            patch("swing_trading.cli.validate_pretrade_shadow", return_value=(["TAMPERED"], {})),
            patch("swing_trading.cli.Path.is_file", return_value=True),
            patch("swing_trading.cli.RealtimeQuote.fetch", return_value=quote_snapshot),
            patch("swing_trading.cli.TradeHTMLReporter.generate") as report,
            patch("swing_trading.cli.save_execution_feedback"),
            patch("swing_trading.cli.save_live_performance"),
            patch("swing_trading.cli.save_execution_plan") as plan_save,
        ):
            main_cli()
        orders = report.call_args.args[1]
        self.assertEqual([], orders["buy_orders"])
        self.assertEqual([], orders["sell_orders"])
        self.assertFalse(orders["rotation_source"]["pretrade_shadow"]["valid"])
        self.assertEqual(
            ["TAMPERED"],
            orders["rotation_source"]["pretrade_shadow"]["errors"],
        )
        self.assertEqual(
            "SOURCE_BLOCKED",
            orders["decision_diagnostics"]["reasons"][0]["code"],
        )
        plan_save.assert_not_called()

    def test_feedback_only_never_loads_state_quotes_or_rotation(self):
        orders = {
            "run_date": "2026-07-20",
            "data_date": "2026-07-17",
            "execution_date": "2026-07-20",
            "model_version": "rotation-v2-test",
            "execution_policy_version": "single-exposure-authority-v4",
            "acceptance_policy_version": "rolling-excess-stability-v1",
            "strategy_specification_fingerprint": "spec-test",
            "decision_diagnostics": {"plan_id": "rotation-v2-test:plan"},
            "rotation_source": {"state_write_allowed": True, "quotes": {"tradeable": True}},
            "buy_orders": [{"side": "BUY", "code": "512800", "shares": 100, "price": 10.0, "gross": 1000.0, "total_cost": 1.0}],
            "sell_orders": [],
            "execution_cost_this_run": 1.0,
            "execution_cost_model": {"commission_rate": 0.00015},
            "capacity_summary": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(build_execution_plan(orders)), encoding="utf-8")
            fills_path = root / "fills.json"
            fills_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "broker_confirmed": True,
                    "broker": "test-broker",
                    "plan_id": "rotation-v2-test:plan",
                    "execution_date": "2026-07-20",
                    "fills": [{"code": "512800", "side": "BUY", "shares": 100, "price": 10.01, "commission": 0.15, "other_fees": 0.04, "trade_date": "2026-07-20"}],
                }),
                encoding="utf-8",
            )
            feedback_path = root / "feedback.json"
            feedback_history_path = root / "feedback_history.json"
            with (
                patch.object(sys, "argv", ["swing-trading", "--feedback-only", "--execution-plan", str(plan_path), "--broker-fills", str(fills_path)]),
                patch.dict(os.environ, {"AUTO_GIT_PUSH": "false"}),
                patch("swing_trading.cli.ensure_directories"),
                patch("swing_trading.cli._configure_logging"),
                patch("swing_trading.cli.execution_feedback_path", return_value=feedback_path),
                patch(
                    "swing_trading.cli.execution_feedback_history_path",
                    return_value=feedback_history_path,
                ),
                patch("swing_trading.cli.StateManager.load") as state_load,
                patch("swing_trading.cli.RotationSource.load") as rotation_load,
                patch("swing_trading.cli.RealtimeQuote.fetch") as quote_fetch,
            ):
                main_cli()
            value = json.loads(feedback_path.read_text(encoding="utf-8"))
        self.assertEqual("BROKER_CONFIRMED", value["evidence_level"])
        state_load.assert_not_called()
        rotation_load.assert_not_called()
        quote_fetch.assert_not_called()

    def test_no_realtime_mode_can_never_rebalance(self):
        payload = rotation()
        for item in payload["top_candidates"]:
            item["price"] = 10.0
        snapshot = RotationSnapshot(
            payload=payload,
            source="local",
            source_url="local-test",
            fetched_at="2026-07-19 10:00:00",
            allow_rebalance=True,
        )
        state = StateManager.initial()

        with (
            patch.object(sys, "argv", ["swing-trading", "--no-realtime"]),
            patch.dict(os.environ, {"AUTO_GIT_PUSH": "true"}),
            patch("swing_trading.cli.ensure_directories"),
            patch("swing_trading.cli._configure_logging"),
            patch("swing_trading.cli.StateManager.load", return_value=state),
            patch("swing_trading.cli.StateManager.save") as save,
            patch("swing_trading.cli.RotationSource.load", return_value=snapshot),
            patch("swing_trading.cli.TradeHTMLReporter.generate") as report,
            patch("swing_trading.publish.publish_execution_outputs") as publish,
        ):
            main_cli()

        orders = report.call_args.args[1]
        self.assertEqual([], orders["buy_orders"])
        self.assertEqual([], orders["sell_orders"])
        self.assertEqual([], state["holdings"])
        self.assertEqual([], state["trade_history"])
        self.assertEqual("SOURCE_BLOCKED", orders["decision_diagnostics"]["reasons"][0]["code"])
        self.assertFalse(orders["rotation_source"]["quotes"]["tradeable"])
        self.assertIn("REALTIME_QUOTES_DISABLED", orders["rotation_source"]["quotes"]["errors"])
        save.assert_not_called()
        publish.assert_not_called()
        self.assertTrue(str(report.call_args.kwargs["output_path"]).endswith("reports\\dry_run.html"))

    def test_invalid_realtime_quotes_never_overwrite_state_or_update_drawdown(self):
        payload = rotation()
        snapshot = RotationSnapshot(
            payload=payload,
            source="local",
            source_url="local-test",
            fetched_at="2026-07-20 10:00:00",
            allow_rebalance=True,
        )
        state = StateManager.initial()
        state["cash"] = 9000.0
        state["holdings"] = [
            {"code": "588170", "name": "ETF", "shares": 100, "buy_price": 10.0}
        ]
        prior = dict(state)
        invalid_quotes = QuoteSnapshot(
            prices={"588170": 5.0, "512800": 5.0, "513120": 5.0},
            quote_dates={code: "2026-07-20" for code in payload["target_weights"]},
            quote_times={code: "10:00:00" for code in payload["target_weights"]},
            requested_codes=list(payload["target_weights"]),
            fetched_at="2026-07-20 14:00:00+0800",
        )
        with (
            patch.object(sys, "argv", ["swing-trading", "--run-date", payload["execution_date"]]),
            patch.dict(os.environ, {"AUTO_GIT_PUSH": "false"}),
            patch("swing_trading.cli.ensure_directories"),
            patch("swing_trading.cli._configure_logging"),
            patch("swing_trading.cli.StateManager.load", return_value=state),
            patch("swing_trading.cli.StateManager.save") as save,
            patch("swing_trading.cli.RotationSource.load", return_value=snapshot),
            patch("swing_trading.cli.RealtimeQuote.fetch", return_value=invalid_quotes),
            patch("swing_trading.cli.TradeHTMLReporter.generate") as report,
            patch("swing_trading.cli.save_execution_feedback"),
        ):
            main_cli()
        orders = report.call_args.args[1]
        self.assertEqual([], orders["buy_orders"])
        self.assertEqual([], orders["sell_orders"])
        self.assertIsNone(orders["total_assets"])
        self.assertEqual(prior["peak_capital"], state["peak_capital"])
        self.assertEqual(prior["max_drawdown"], state["max_drawdown"])
        self.assertEqual(prior["last_run"], state["last_run"])
        self.assertFalse(orders["rotation_source"]["state_write_allowed"])
        self.assertEqual({}, report.call_args.args[2])
        save.assert_not_called()

    def test_daily_valuation_after_execution_date_updates_state_without_rebalancing(self):
        payload = rotation(
            data_date="2026-07-17",
            generated_at="2026-07-19 15:30:00",
            execution_date="2026-07-20",
        )
        snapshot = RotationSnapshot(
            payload=payload,
            source="local",
            source_url="local-test",
            fetched_at="2026-07-21 14:55:30",
            allow_rebalance=True,
        )
        quote_snapshot = Mock()
        quote_snapshot.prices = {
            "510300": 4.2,
            "512800": 1.3,
            "513120": 0.9,
            "588170": 1.1,
        }
        quote_snapshot.diagnostics.return_value = {
            "mode": "SINA_REALTIME",
            "tradeable": False,
            "errors": [
                "RUN_DATE_NOT_EXECUTION_DATE:run=2026-07-21,execution=2026-07-20"
            ],
        }
        quote_snapshot.valuation_diagnostics.return_value = {
            "mode": "DAILY_MARK_TO_MARKET",
            "valuation_date": "2026-07-21",
            "tradeable": True,
            "errors": [],
        }
        state = StateManager.initial()
        state["cash"] = 8_900.0
        state["holdings"] = [
            {
                "code": "588170",
                "name": "ETF",
                "shares": 1_000,
                "buy_price": 1.0,
                "buy_date": "2026-07-20",
            }
        ]
        state["last_model_version"] = "rotation-v2-prior-portfolio"

        with (
            patch.object(sys, "argv", ["swing-trading", "--run-date", "2026-07-21"]),
            patch.dict(os.environ, {"AUTO_GIT_PUSH": "false"}),
            patch("swing_trading.cli.ensure_directories"),
            patch("swing_trading.cli._configure_logging"),
            patch("swing_trading.cli.StateManager.load", return_value=state),
            patch("swing_trading.cli.StateManager.save") as state_save,
            patch("swing_trading.cli.RotationSource.load", return_value=snapshot),
            patch("swing_trading.cli.RealtimeQuote.fetch", return_value=quote_snapshot),
            patch("swing_trading.cli.TradeHTMLReporter.generate") as report,
            patch("swing_trading.cli.save_execution_feedback"),
            patch("swing_trading.cli.save_live_performance") as performance_save,
            patch("swing_trading.cli.save_execution_plan") as plan_save,
        ):
            main_cli()

        orders = report.call_args.args[1]
        self.assertEqual([], orders["buy_orders"])
        self.assertEqual([], orders["sell_orders"])
        self.assertEqual(10_000.0, orders["total_assets"])
        self.assertEqual("2026-07-21", state["last_run"])
        self.assertFalse(orders["rotation_source"]["quotes"]["tradeable"])
        self.assertTrue(
            orders["rotation_source"]["valuation_quotes"]["tradeable"]
        )
        self.assertEqual(
            "rotation-v2-prior-portfolio",
            performance_save.call_args.args[0]["model_version"],
        )
        self.assertNotEqual(
            payload["model_version"],
            performance_save.call_args.args[0]["model_version"],
        )
        self.assertEqual(
            "SOURCE_BLOCKED",
            orders["decision_diagnostics"]["reasons"][0]["code"],
        )
        state_save.assert_called_once()
        performance_save.assert_called_once()
        plan_save.assert_not_called()




    def test_live_run_writes_public_feedback_and_history(self):
        payload = rotation()
        snapshot = RotationSnapshot(
            payload=payload,
            source="local",
            source_url="local-test",
            fetched_at="2026-07-20 09:35:00",
            allow_rebalance=True,
        )
        quote_snapshot = Mock()
        quote_snapshot.prices = {code: 10.0 for code in payload["target_weights"]}
        quote_snapshot.prices["510300"] = 4.0
        quote_snapshot.diagnostics.return_value = {
            "mode": "SINA_REALTIME",
            "tradeable": True,
            "errors": [],
        }
        quote_snapshot.valuation_diagnostics.return_value = {
            "mode": "DAILY_MARK_TO_MARKET",
            "tradeable": True,
            "errors": [],
        }
        state = StateManager.initial()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            public.mkdir()
            feedback_path = public / "execution_feedback_latest.json"
            history_path = public / "execution_feedback_history.json"
            performance_path = public / "live_performance_latest.json"
            with (
                patch.object(
                    sys,
                    "argv",
                    ["swing-trading", "--run-date", payload["execution_date"]],
                ),
                patch.dict(os.environ, {"AUTO_GIT_PUSH": "false"}),
                patch("swing_trading.cli.ensure_directories"),
                patch("swing_trading.cli._configure_logging"),
                patch("swing_trading.cli.StateManager.load", return_value=state),
                patch("swing_trading.cli.StateManager.save"),
                patch("swing_trading.cli.RotationSource.load", return_value=snapshot),
                patch("swing_trading.cli.RealtimeQuote.fetch", return_value=quote_snapshot),
                patch("swing_trading.cli.TradeHTMLReporter.generate"),
                patch("swing_trading.cli.save_execution_plan"),
                patch(
                    "swing_trading.cli.execution_feedback_path",
                    return_value=feedback_path,
                ),
                patch(
                    "swing_trading.cli.execution_feedback_history_path",
                    return_value=history_path,
                ),
                patch(
                    "swing_trading.cli.live_performance_path",
                    return_value=performance_path,
                ),
            ):
                main_cli()
            latest = json.loads(feedback_path.read_text(encoding="utf-8"))
            history = json.loads(history_path.read_text(encoding="utf-8"))
            performance = json.loads(performance_path.read_text(encoding="utf-8"))
        self.assertTrue(feedback_path.exists() or latest)
        self.assertEqual(1, history["event_count"])
        self.assertEqual(latest["feedback_id"], history["events"][0]["feedback_id"])
        self.assertEqual(payload["model_version"], performance["model_version"])
        self.assertGreaterEqual(performance["observation_count"], 1)


if __name__ == "__main__":
    unittest.main()
