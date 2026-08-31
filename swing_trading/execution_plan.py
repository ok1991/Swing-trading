"""Immutable execution-plan artifacts for delayed broker-fill reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from .state import atomic_json_save


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_execution_plan(
    orders: Mapping[str, Any],
    *,
    generated_at: str = "",
    pre_trade_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    planned_orders = list(orders.get("buy_orders") or []) + list(
        orders.get("sell_orders") or []
    )
    if not planned_orders:
        raise ValueError("execution plan requires at least one order")
    plan_id = str((orders.get("decision_diagnostics") or {}).get("plan_id", ""))
    if not plan_id:
        raise ValueError("execution plan has no plan_id")
    payload = dict(orders)
    payload_sha256 = _canonical_hash(payload)
    result = {
        "schema_version": 2 if pre_trade_state is not None else 1,
        "generated_at": generated_at
        or datetime.now().astimezone().isoformat(timespec="seconds"),
        "plan_id": plan_id,
        "model_version": str(orders.get("model_version", "")),
        "execution_date": str(orders.get("execution_date", ""))[:10],
        "order_count": len(planned_orders),
        "payload_sha256": payload_sha256,
        "orders": payload,
    }
    if pre_trade_state is not None:
        state_payload = dict(pre_trade_state)
        result["pre_trade_state_sha256"] = _canonical_hash(state_payload)
        result["pre_trade_state"] = state_payload
    return result


def validate_execution_plan(value: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    schema_version = int(value.get("schema_version", 0) or 0)
    if schema_version not in {1, 2}:
        errors.append("EXECUTION_PLAN_SCHEMA_MISMATCH")
    orders = value.get("orders")
    if not isinstance(orders, dict):
        return errors + ["EXECUTION_PLAN_ORDERS_INVALID"]
    plan_id = str((orders.get("decision_diagnostics") or {}).get("plan_id", ""))
    if not plan_id or plan_id != str(value.get("plan_id", "")):
        errors.append("EXECUTION_PLAN_ID_MISMATCH")
    planned_orders = list(orders.get("buy_orders") or []) + list(
        orders.get("sell_orders") or []
    )
    if not planned_orders or len(planned_orders) != int(value.get("order_count", 0) or 0):
        errors.append("EXECUTION_PLAN_ORDER_COUNT_MISMATCH")
    if str(value.get("model_version", "")) != str(orders.get("model_version", "")):
        errors.append("EXECUTION_PLAN_MODEL_MISMATCH")
    if str(value.get("execution_date", ""))[:10] != str(
        orders.get("execution_date", "")
    )[:10]:
        errors.append("EXECUTION_PLAN_DATE_MISMATCH")
    if str(value.get("payload_sha256", "")) != _canonical_hash(orders):
        errors.append("EXECUTION_PLAN_FINGERPRINT_MISMATCH")
    if schema_version == 2:
        pre_trade_state = value.get("pre_trade_state")
        if not isinstance(pre_trade_state, dict):
            errors.append("EXECUTION_PLAN_PRE_TRADE_STATE_INVALID")
        elif str(value.get("pre_trade_state_sha256", "")) != _canonical_hash(
            pre_trade_state
        ):
            errors.append("EXECUTION_PLAN_PRE_TRADE_STATE_FINGERPRINT_MISMATCH")
    return list(dict.fromkeys(errors))


def _archive_name(plan: Mapping[str, Any]) -> str:
    identifier = hashlib.sha256(str(plan.get("plan_id", "")).encode("utf-8")).hexdigest()[:24]
    execution_date = str(plan.get("execution_date", ""))[:10].replace("-", "") or "unknown"
    return f"{execution_date}-{identifier}.json"


def save_execution_plan(
    value: Mapping[str, Any],
    latest_path: Path,
    archive_dir: Path,
) -> Tuple[Path, Path]:
    errors = validate_execution_plan(value)
    if errors:
        raise ValueError("invalid execution plan: " + ", ".join(errors))
    latest_path = Path(latest_path)
    archive_path = Path(archive_dir) / _archive_name(value)
    atomic_json_save(dict(value), archive_path)
    atomic_json_save(dict(value), latest_path)
    return latest_path, archive_path


def load_execution_plan(path: Path) -> Dict[str, Any]:
    return dict(load_execution_plan_artifact(path)["orders"])


def load_execution_plan_artifact(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"execution plan is unreadable: {str(error)[:200]}") from error
    if not isinstance(value, dict):
        raise RuntimeError("execution plan must be a JSON object")
    errors = validate_execution_plan(value)
    if errors:
        raise RuntimeError("execution plan failed integrity checks: " + ", ".join(errors))
    return dict(value)


__all__ = [
    "build_execution_plan",
    "load_execution_plan",
    "load_execution_plan_artifact",
    "save_execution_plan",
    "validate_execution_plan",
]
