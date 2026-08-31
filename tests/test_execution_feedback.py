import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from swing_trading.execution_feedback import (
    BROKER_CONFIRMED,
    BROKER_EVIDENCE_REJECTED,
    MODEL_ESTIMATE_ONLY,
    NO_ORDERS,
    build_execution_feedback,
    build_virtual_fills_from_plan,
    save_execution_feedback,
)


def sample_orders():
    return {
        "run_date": "2026-07-20",
        "data_date": "2026-07-17",
        "execution_date": "2026-07-20",
        "model_version": "rotation-v2-test",
        "execution_policy_version": "single-exposure-authority-v4",
        "acceptance_policy_version": "rolling-excess-stability-v1",
        "strategy_specification_fingerprint": "spec-test",
        "decision_diagnostics": {
            "plan_id": "rotation-v2-test:plan",
            "rebalance_required": True,
            "reasons": [],
        },
        "rotation_source": {
            "state_write_allowed": True,
            "quotes": {"tradeable": True},
        },
        "buy_orders": [
            {
                "side": "BUY",
                "code": "512800",
                "shares": 100,
                "price": 10.0,
                "gross": 1000.0,
                "total_cost": 1.0,
                "unfilled_shares": 0,
            }
        ],
        "sell_orders": [],
        "execution_cost_this_run": 1.0,
        "execution_cost_model": {"commission_rate": 0.00015},
        "capacity_summary": {"buy_fill_ratio": 1.0},
    }


class ExecutionFeedbackTests(unittest.TestCase):
    def test_virtual_fills_use_explicit_contract_fees(self):
        value = build_virtual_fills_from_plan(sample_orders())
        jsonschema.validate(
            value,
            json.loads(
                (Path(__file__).resolve().parents[1] / "contracts" / "broker_fills_v1.schema.json").read_text(
                    encoding="utf-8"
                )
            ),
        )
        self.assertEqual("virtual-paper", value["broker"])
        self.assertEqual("rotation-v2-test:plan", value["plan_id"])
        self.assertEqual("2026-07-20", value["execution_date"])
        self.assertEqual(10.0, value["fills"][0]["price"])

    def test_virtual_fills_reject_empty_or_invalid_orders(self):
        orders = sample_orders()
        orders["buy_orders"] = []
        with self.assertRaises(ValueError):
            build_virtual_fills_from_plan(orders)

    def test_model_estimate_cannot_claim_broker_confirmation(self):
        value = build_execution_feedback(sample_orders(), generated_at="2026-07-20T10:00:00+08:00")
        self.assertEqual(MODEL_ESTIMATE_ONLY, value["evidence_level"])
        self.assertFalse(value["broker_confirmed"])
        self.assertEqual(1.0, value["estimated_execution_cost"])
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "contracts" / "execution_feedback_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.validate(value, schema)

    def test_no_orders_is_explicit(self):
        orders = sample_orders()
        orders["buy_orders"] = []
        orders["execution_cost_this_run"] = 0.0
        orders["decision_diagnostics"] = {
            "plan_id": "rotation-v2-test:plan",
            "rebalance_required": True,
            "reasons": [
                {
                    "code": "PORTFOLIO_ALREADY_AT_TARGET",
                    "message": "already aligned",
                }
            ],
        }
        value = build_execution_feedback(orders)
        self.assertEqual(NO_ORDERS, value["evidence_level"])
        self.assertTrue(value["rebalance_required"])
        self.assertEqual(
            ["PORTFOLIO_ALREADY_AT_TARGET"], value["decision_reason_codes"]
        )

    def test_blocked_zero_orders_preserve_failure_reason(self):
        orders = sample_orders()
        orders["buy_orders"] = []
        orders["execution_cost_this_run"] = 0.0
        orders["decision_diagnostics"] = {
            "plan_id": "rotation-v2-test:plan",
            "rebalance_required": False,
            "reasons": [{"code": "SOURCE_BLOCKED", "message": "blocked"}],
        }
        value = build_execution_feedback(orders)
        self.assertEqual(NO_ORDERS, value["evidence_level"])
        self.assertFalse(value["rebalance_required"])
        self.assertEqual(["SOURCE_BLOCKED"], value["decision_reason_codes"])

    def test_history_preserves_unique_events_when_latest_is_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = Path(directory) / "latest.json"
            history = Path(directory) / "history.json"
            estimate = build_execution_feedback(
                sample_orders(), generated_at="2026-07-20T09:35:00+08:00"
            )
            save_execution_feedback(estimate, latest, history)
            no_orders = sample_orders()
            no_orders["buy_orders"] = []
            no_orders["execution_cost_this_run"] = 0.0
            no_orders["decision_diagnostics"] = {
                "plan_id": "rotation-v2-test:plan",
                "rebalance_required": False,
                "reasons": [
                    {"code": "PLAN_ALREADY_APPLIED", "message": "already applied"}
                ],
            }
            empty = build_execution_feedback(
                no_orders, generated_at="2026-07-20T13:05:00+08:00"
            )
            save_execution_feedback(empty, latest, history)
            save_execution_feedback(empty, latest, history)
            ledger = json.loads(history.read_text(encoding="utf-8"))
            current = json.loads(latest.read_text(encoding="utf-8"))
        self.assertEqual(2, ledger["event_count"])
        self.assertEqual(
            {MODEL_ESTIMATE_ONLY, NO_ORDERS},
            {item["evidence_level"] for item in ledger["events"]},
        )
        self.assertEqual(NO_ORDERS, current["evidence_level"])

    def test_wrong_plan_broker_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fills.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "broker_confirmed": True,
                        "broker": "test-broker",
                        "plan_id": "wrong-plan",
                        "execution_date": "2026-07-20",
                        "fills": [
                            {
                                "code": "512800",
                                "side": "BUY",
                                "shares": 100,
                                "price": 10.01,
                                "commission": 0.15,
                                "other_fees": 0.04,
                                "trade_date": "2026-07-20",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            value = build_execution_feedback(sample_orders(), broker_fills_path=path)
        self.assertEqual(BROKER_EVIDENCE_REJECTED, value["evidence_level"])
        self.assertIn("BROKER_PLAN_ID_MISMATCH", value["rejection_reasons"])
        self.assertFalse(value["broker_confirmed"])

    def test_empty_plan_cannot_be_promoted_by_empty_broker_file(self):
        orders = sample_orders()
        orders["buy_orders"] = []
        orders["execution_cost_this_run"] = 0.0
        orders["decision_diagnostics"] = {
            "plan_id": "rotation-v2-test:plan",
            "rebalance_required": False,
            "reasons": [
                {
                    "code": "PLAN_AWAITING_BROKER_CONFIRMATION",
                    "message": "awaiting broker confirmation",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fills.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "broker_confirmed": True,
                        "broker": "test-broker",
                        "plan_id": "rotation-v2-test:plan",
                        "execution_date": "2026-07-20",
                        "fills": [],
                    }
                ),
                encoding="utf-8",
            )
            value = build_execution_feedback(orders, broker_fills_path=path)
        self.assertEqual(BROKER_EVIDENCE_REJECTED, value["evidence_level"])
        self.assertFalse(value["broker_confirmed"])
        self.assertIn(
            "BROKER_EVIDENCE_WITHOUT_PLANNED_ORDERS",
            value["rejection_reasons"],
        )

    def test_valid_broker_file_computes_real_cost_deviation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fills.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "broker_confirmed": True,
                        "broker": "test-broker",
                        "account_reference_hash": "account-hash",
                        "exported_at": "2026-07-20T15:10:00+08:00",
                        "plan_id": "rotation-v2-test:plan",
                        "execution_date": "2026-07-20",
                        "fills": [
                            {
                                "code": "512800",
                                "side": "BUY",
                                "shares": 100,
                                "price": 10.01,
                                "commission": 0.15,
                                "other_fees": 0.04,
                                "trade_date": "2026-07-20",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            value = build_execution_feedback(sample_orders(), broker_fills_path=path)
        self.assertEqual(BROKER_CONFIRMED, value["evidence_level"])
        self.assertTrue(value["broker_confirmed"])
        self.assertAlmostEqual(1.19, value["broker_evidence"]["actual_total_cost"], places=6)
        self.assertAlmostEqual(1.19, value["broker_evidence"]["actual_to_expected_cost_ratio"], places=6)

    def test_partial_fill_uses_only_executed_quantity_for_cost_comparison(self):
        orders = sample_orders()
        orders["buy_orders"][0].update(
            {"shares": 200, "gross": 2000.0, "total_cost": 2.0}
        )
        orders["execution_cost_this_run"] = 2.0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fills.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "broker_confirmed": True,
                        "broker": "test-broker",
                        "plan_id": "rotation-v2-test:plan",
                        "execution_date": "2026-07-20",
                        "fills": [
                            {
                                "code": "512800",
                                "side": "BUY",
                                "shares": 100,
                                "price": 10.01,
                                "commission": 0.15,
                                "other_fees": 0.04,
                                "trade_date": "2026-07-20",
                            }
                        ],
                        "order_outcomes": [
                            {
                                "code": "512800",
                                "side": "BUY",
                                "status": "PARTIALLY_FILLED",
                                "filled_shares": 100,
                                "unfilled_shares": 100,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            value = build_execution_feedback(orders, broker_fills_path=path)
        self.assertEqual(BROKER_CONFIRMED, value["evidence_level"])
        self.assertEqual("PARTIAL", value["broker_fill_completion_status"])
        self.assertEqual(1.0, value["broker_evidence"]["expected_model_cost"])
        self.assertAlmostEqual(1.19, value["broker_evidence"]["actual_total_cost"], places=6)

    def test_explicit_unfilled_outcome_is_confirmed_without_cost_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fills.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "broker_confirmed": True,
                        "broker": "test-broker",
                        "plan_id": "rotation-v2-test:plan",
                        "execution_date": "2026-07-20",
                        "fills": [],
                        "order_outcomes": [
                            {
                                "code": "512800",
                                "side": "BUY",
                                "status": "UNFILLED",
                                "filled_shares": 0,
                                "unfilled_shares": 100,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            value = build_execution_feedback(sample_orders(), broker_fills_path=path)
        self.assertEqual(BROKER_CONFIRMED, value["evidence_level"])
        self.assertEqual("UNFILLED", value["broker_fill_completion_status"])
        self.assertEqual(0.0, value["broker_evidence"]["broker_gross"])
        self.assertIsNone(value["broker_evidence"]["actual_to_expected_cost_ratio"])


if __name__ == "__main__":
    unittest.main()
