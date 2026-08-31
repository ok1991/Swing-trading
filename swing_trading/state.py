"""Portfolio state persistence with migration from the former V4 executor."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from .config import INITIAL_CAPITAL, STATE_SCHEMA_VERSION


def atomic_json_save(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class StateManager:
    @staticmethod
    def initial() -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "strategy": "approved_rotation_only",
            "initial_capital": INITIAL_CAPITAL,
            "cash": INITIAL_CAPITAL,
            "holdings": [],
            "trade_history": [],
            "peak_capital": INITIAL_CAPITAL,
            "max_drawdown": 0.0,
            "last_run": "",
            "last_plan_id": "",
            "last_model_version": "",
            "pending_broker_confirmation_plan_id": "",
            "last_execution_satisfied_plan_id": "",
            "cumulative_execution_cost": 0.0,
            "broker_reconciled_feedback_ids": [],
            "last_broker_reconciliation": {},
            "performance_baseline": {},
            "performance_history": [],
            "live_performance": {},
        }

    @classmethod
    def load(cls, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return cls.initial()
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") == 4:
            value["schema_version"] = STATE_SCHEMA_VERSION
            value["strategy"] = "approved_rotation_only"
            value["last_plan_id"] = ""
            value["last_model_version"] = ""
            value["cumulative_execution_cost"] = 0.0
            for holding in value.get("holdings", []):
                holding["source"] = "legacy_v4_migration"
        elif value.get("schema_version") == 5:
            value["schema_version"] = STATE_SCHEMA_VERSION
            value["broker_reconciled_feedback_ids"] = []
            value["last_broker_reconciliation"] = {}
            value["performance_baseline"] = {}
            value["performance_history"] = []
            value["live_performance"] = {}
        elif value.get("schema_version") == 6:
            value["schema_version"] = STATE_SCHEMA_VERSION
            value["performance_baseline"] = {}
            value["performance_history"] = []
            value["live_performance"] = {}
        elif value.get("schema_version") == 7:
            value["schema_version"] = STATE_SCHEMA_VERSION
        elif value.get("schema_version") != STATE_SCHEMA_VERSION:
            raise RuntimeError("状态文件版本无法迁移；请先备份 runtime/state/portfolio_state.json")
        value.setdefault("holdings", [])
        value.setdefault("trade_history", [])
        value.setdefault("last_plan_id", "")
        value.setdefault("last_model_version", "")
        if (
            "pending_broker_confirmation_plan_id" not in value
            and "last_execution_satisfied_plan_id" not in value
        ):
            last_plan_id = str(value.get("last_plan_id", ""))
            reconciliation = dict(value.get("last_broker_reconciliation") or {})
            reconciled_plan_id = str(reconciliation.get("plan_id", ""))
            reconciled = bool(
                last_plan_id
                and reconciled_plan_id == last_plan_id
                and str(reconciliation.get("status", ""))
                in {"APPLIED", "ALREADY_APPLIED"}
            )
            value["pending_broker_confirmation_plan_id"] = (
                "" if reconciled else last_plan_id
            )
            value["last_execution_satisfied_plan_id"] = (
                last_plan_id if reconciled else ""
            )
        value.setdefault("pending_broker_confirmation_plan_id", "")
        value.setdefault("last_execution_satisfied_plan_id", "")
        value.setdefault("cumulative_execution_cost", 0.0)
        value.setdefault("broker_reconciled_feedback_ids", [])
        value.setdefault("last_broker_reconciliation", {})
        value.setdefault("performance_baseline", {})
        value.setdefault("performance_history", [])
        value.setdefault("live_performance", {})
        return value

    @staticmethod
    def save(state: Dict[str, Any], path: Path) -> None:
        atomic_json_save(state, path)


__all__ = ["StateManager", "atomic_json_save"]
