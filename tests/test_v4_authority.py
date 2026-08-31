import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import main as trading
from jsonschema import Draft202012Validator
from swing_trading.costs import DEFAULT_COST_MODEL, ExecutionCostModel


def rotation(**overrides):
    value = {
        "schema_version": 2,
        "data_date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_rebalance_week": "2026-W29",
        "sleeves": [["588170", "512800", "513120"], ["588170", "512800", "513120"]],
        "target_weights": {"588170": 0.333333, "512800": 0.333333, "513120": 0.333333},
        "execution_liquidity": {},
        "max_exposure_ratio": 1.0,
        "cash_weight": 0.0,
        "capacity_reference_capital": 10000.0,
        "market_policy": {"state": "NORMAL", "entry_permission": "TRADEABLE", "max_exposure_ratio": 1.0},
        "top_candidates": [
            {"code": "588170", "name": "半导体ETF"},
            {"code": "512800", "name": "银行ETF"},
            {"code": "513120", "name": "创新药ETF"},
        ],
        "approved": True,
        "model_version": "rotation-test-aaaaaaaa",
        "execution_policy_version": "single-exposure-authority-v4",
        "acceptance_policy_version": "rolling-excess-stability-v1",
        "exposure_authority": "v4_market_policy",
        "strategy_specification_fingerprint": "a" * 64,
        "walk_forward_metrics": {
            "information_ratio": 0.5,
            "capacity_truncation_count": 0,
            "requested_buy_value": 10000.0,
            "executed_buy_value": 10000.0,
            "capacity_truncated_buy_value": 0.0,
            "unfilled_buy_value": 0.0,
            "buy_fill_ratio": 1.0,
            "capacity_fill_ratio": 1.0,
            "cost_model": {
                **DEFAULT_COST_MODEL.to_dict(),
                "transfer_fee_rate": 0.0,
                "stamp_duty_sell_rate": 0.0,
            },
        },
    }
    value.update(overrides)
    if "execution_liquidity" not in overrides:
        value["execution_liquidity"] = {
            str(code): {
                "average_daily_amount_20": 1_000_000.0,
                "max_new_risk_amount": 100_000.0,
                "max_participation_rate": 0.1,
                "as_of_date": str(value["data_date"])[:10],
            }
            for code in (value.get("target_weights") or {})
        }
    if "execution_date" not in overrides:
        execution = datetime.strptime(str(value["data_date"])[:10], "%Y-%m-%d") + timedelta(days=1)
        while execution.weekday() >= 5:
            execution += timedelta(days=1)
        value["execution_date"] = execution.strftime("%Y-%m-%d")
    return value


class RotationAuthorityTests(unittest.TestCase):
    def test_only_approved_rotation_with_matching_fee_contract_is_accepted(self):
        self.assertEqual([], trading.validate_rotation_contract(rotation()))
        self.assertTrue(trading.validate_rotation_contract(rotation(approved=False)))
        wrong = rotation()
        wrong["walk_forward_metrics"]["cost_model"]["commission_rate"] = 0.0003
        self.assertTrue(any("不一致" in item for item in trading.validate_rotation_contract(wrong)))
        wrong_slippage = rotation()
        wrong_slippage["walk_forward_metrics"]["cost_model"]["base_slippage_bps"] = 9.0
        self.assertTrue(
            any("base_slippage_bps" in item for item in trading.validate_rotation_contract(wrong_slippage))
        )
        missing_liquidity = rotation()
        missing_liquidity["execution_liquidity"].pop("588170")
        self.assertTrue(
            any("588170" in item and "流动性" in item for item in trading.validate_rotation_contract(missing_liquidity))
        )
        old_policy = rotation(execution_policy_version="legacy-no-capacity")
        self.assertTrue(
            any("执行政策版本" in item for item in trading.validate_rotation_contract(old_policy))
        )
        old_acceptance = rotation(acceptance_policy_version="aggregate-only-v0")
        self.assertTrue(
            any("验收政策版本" in item for item in trading.validate_rotation_contract(old_acceptance))
        )
        wrong_fingerprint = rotation(
            strategy_specification_fingerprint="b" * 64,
        )
        self.assertTrue(
            any("model_version" in item and "指纹" in item for item in trading.validate_rotation_contract(wrong_fingerprint))
        )

    def test_target_weights_are_the_only_buy_authority(self):
        state = trading.StateManager.initial()
        prices = {"588170": 10.0, "512800": 10.0, "513120": 10.0}
        payload = rotation()
        engine = trading.TradingEngine(state, payload, prices, payload["execution_date"])
        orders = engine.run()
        self.assertEqual(
            payload["walk_forward_metrics"]["cost_model"],
            orders["execution_cost_model"],
        )
        self.assertEqual({"512800", "513120", "588170"}, {item["code"] for item in orders["buy_orders"]})
        self.assertTrue(all(item["commission"] < 5.0 for item in orders["buy_orders"]))
        self.assertTrue(all(item["commission"] > 0.0 for item in orders["buy_orders"]))
        self.assertTrue(all(0.0 < item["participation_rate"] < 0.01 for item in orders["buy_orders"]))
        self.assertTrue(all(item["average_daily_amount"] == 1_000_000.0 for item in orders["buy_orders"]))

    def test_engine_cannot_execute_before_designated_session(self):
        payload = rotation()
        state = trading.StateManager.initial()
        prices = {"588170": 10.0, "512800": 10.0, "513120": 10.0}
        orders = trading.TradingEngine(state, payload, prices, payload["data_date"]).run()
        self.assertEqual([], orders["buy_orders"])
        self.assertEqual("EXECUTION_DATE_MISMATCH", orders["decision_diagnostics"]["reasons"][0]["code"])

    def test_engine_cost_override_must_match_approved_backtest(self):
        payload = rotation()
        state = trading.StateManager.initial()
        prices = {"588170": 10.0, "512800": 10.0, "513120": 10.0}
        mismatched = ExecutionCostModel(base_slippage_bps=9.0)
        orders = trading.TradingEngine(
            state,
            payload,
            prices,
            payload["execution_date"],
            cost_model=mismatched,
        ).run()
        self.assertEqual([], orders["buy_orders"])
        self.assertEqual("ENGINE_COST_MODEL_MISMATCH", orders["decision_diagnostics"]["reasons"][0]["code"])

    def test_new_risk_is_hard_capped_at_ten_percent_adv(self):
        payload = rotation()
        state = trading.StateManager.initial()
        state.update(
            {
                "initial_capital": 100_000_000.0,
                "cash": 100_000_000.0,
                "peak_capital": 100_000_000.0,
            }
        )
        prices = {"588170": 10.0, "512800": 10.0, "513120": 10.0}
        orders = trading.TradingEngine(
            state, payload, prices, payload["execution_date"]
        ).run()
        self.assertEqual(3, len(orders["buy_orders"]))
        self.assertTrue(all(item["shares"] == 10_000 for item in orders["buy_orders"]))
        self.assertTrue(all(item["liquidity_capped"] for item in orders["buy_orders"]))
        self.assertTrue(all(item["unfilled_shares"] > 0 for item in orders["buy_orders"]))
        self.assertTrue(all(item["capacity_headroom_amount"] == 0.0 for item in orders["buy_orders"]))
        self.assertTrue(all(item["participation_rate"] <= 0.10 for item in orders["buy_orders"]))
        self.assertFalse(any(item["capacity_exceeded"] for item in orders["buy_orders"]))
        self.assertEqual(
            3,
            sum(
                item["code"] == "LIQUIDITY_CAP_REACHED"
                for item in orders["decision_diagnostics"]["reasons"]
            ),
        )
        self.assertEqual(3, orders["capacity_summary"]["capacity_truncation_count"])
        self.assertGreater(orders["capacity_summary"]["unfilled_new_risk_amount"], 0.0)
        self.assertLess(orders["capacity_summary"]["buy_fill_ratio"], 1.0)

    def test_risk_budget_must_match_target_weight_sum(self):
        mismatch = rotation(max_exposure_ratio=0.5, cash_weight=0.5)
        self.assertTrue(any("target_weights 合计" in item for item in trading.validate_rotation_contract(mismatch)))

    def test_risk_control_cash_target_is_valid(self):
        cash_target = rotation(
            target_weights={},
            max_exposure_ratio=0.0,
            cash_weight=1.0,
            market_policy={"state": "RISK_OFF", "entry_permission": "BLOCKED", "max_exposure_ratio": 0.0},
            exposure_authority="risk_control_fail_closed",
            alpha_model_approved=False,
            risk_control_only=True,
        )
        self.assertEqual([], trading.validate_rotation_contract(cash_target))

    def test_rotation_cannot_override_the_v4_market_exposure(self):
        overridden = rotation()
        overridden["risk_budget_profile"] = {"RISK_OFF": 0.5}
        overridden["market_policy"]["source_max_exposure_ratio"] = 0.0
        errors = trading.validate_rotation_contract(overridden)
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "contracts"
                / "etf_rotation_v2.schema.json"
            ).read_text(encoding="utf-8")
        )
        schema_errors = list(Draft202012Validator(schema).iter_errors(overridden))
        self.assertGreaterEqual(len(schema_errors), 2)
        self.assertTrue(any("覆盖" in item for item in errors))
        self.assertTrue(any("原始仓位" in item for item in errors))

    def test_blocked_market_policy_cannot_publish_positive_exposure(self):
        inconsistent = rotation(
            max_exposure_ratio=0.5,
            cash_weight=0.5,
            target_weights={"588170": 0.5},
            market_policy={
                "state": "RISK_OFF",
                "entry_permission": "BLOCKED",
                "max_exposure_ratio": 0.5,
            },
        )
        errors = trading.validate_rotation_contract(inconsistent)
        self.assertTrue(any("BLOCKED" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
