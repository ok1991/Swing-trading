"""Machine-readable execution feedback with strict broker-evidence validation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .state import atomic_json_save
from .utils import safe_float


MODEL_ESTIMATE_ONLY = "MODEL_ESTIMATE_ONLY"
BROKER_CONFIRMED = "BROKER_CONFIRMED"
BROKER_EVIDENCE_REJECTED = "BROKER_EVIDENCE_REJECTED"
NO_ORDERS = "NO_ORDERS"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    )


def _feedback_identity_payload(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key
        not in {
            "generated_at",
            "feedback_id",
            "state_reconciliation_applied",
            "state_reconciliation",
        }
    }


def execution_feedback_identity_valid(value: Mapping[str, Any]) -> bool:
    expected = str(value.get("feedback_id", ""))
    return bool(
        len(expected) == 64
        and all(char in "0123456789abcdef" for char in expected.lower())
        and expected == _canonical_hash(_feedback_identity_payload(value))
    )


def _planned_orders(orders: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(item)
        for item in list(orders.get("buy_orders") or [])
        + list(orders.get("sell_orders") or [])
    ]


def _aggregate(
    rows: Iterable[Mapping[str, Any]],
    *,
    fill_rows: bool,
) -> Dict[Tuple[str, str], Dict[str, float]]:
    grouped: Dict[Tuple[str, str], Dict[str, float]] = {}
    for row in rows:
        key = (str(row.get("code", "")), str(row.get("side", "")).upper())
        shares = int(row.get("shares", 0) or 0)
        price = safe_float(row.get("price"))
        target = grouped.setdefault(
            key,
            {
                "shares": 0.0,
                "gross": 0.0,
                "commission": 0.0,
                "other_fees": 0.0,
                "model_cost": 0.0,
            },
        )
        target["shares"] += shares
        target["gross"] += shares * price
        if fill_rows:
            target["commission"] += safe_float(row.get("commission"))
            target["other_fees"] += safe_float(row.get("other_fees"))
        else:
            target["model_cost"] += safe_float(row.get("total_cost"))
    return grouped


def _load_broker_evidence(path: Path) -> Tuple[Optional[Dict[str, Any]], str, List[str]]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        return None, "", [f"BROKER_FILE_UNREADABLE:{str(error)[:160]}"]
    digest = _sha256_bytes(raw)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, digest, [f"BROKER_FILE_INVALID_JSON:{str(error)[:160]}"]
    if not isinstance(value, dict):
        return None, digest, ["BROKER_FILE_NOT_OBJECT"]
    return value, digest, []


def build_virtual_fills_from_plan(orders: Mapping[str, Any]) -> Dict[str, Any]:
    planned = _planned_orders(orders)
    if not planned:
        raise ValueError("virtual broker confirmation requires planned orders")
    plan_id = str((orders.get("decision_diagnostics") or {}).get("plan_id", ""))
    execution_date = str(orders.get("execution_date", ""))[:10]
    if not plan_id or not execution_date:
        raise ValueError("virtual broker confirmation requires plan id and execution date")
    fills: List[Dict[str, Any]] = []
    for order in planned:
        shares = int(order.get("shares", 0) or 0)
        gross = safe_float(order.get("gross"))
        commission = safe_float(order.get("commission"))
        other_fees = safe_float(order.get("other_fees"))
        if shares <= 0 or gross <= 0.0 or commission < 0.0 or other_fees < 0.0:
            raise ValueError("virtual broker confirmation found invalid planned order")
        fills.append(
            {
                "code": str(order.get("code", "")),
                "side": str(order.get("side", "")).upper(),
                "shares": shares,
                "price": round(gross / shares, 6),
                "commission": round(commission, 6),
                "other_fees": round(other_fees, 6),
                "trade_date": execution_date,
            }
        )
    return {
        "schema_version": 1,
        "broker_confirmed": True,
        "broker": "virtual-paper",
        "plan_id": plan_id,
        "execution_date": execution_date,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fills": fills,
    }


def _validate_broker_evidence(
    evidence: Mapping[str, Any],
    orders: Mapping[str, Any],
) -> Tuple[List[str], Dict[str, Any]]:
    errors: List[str] = []
    if int(evidence.get("schema_version", 0) or 0) != 1:
        errors.append("BROKER_SCHEMA_VERSION_MISMATCH")
    if evidence.get("broker_confirmed") is not True:
        errors.append("BROKER_CONFIRMATION_MISSING")
    if not str(evidence.get("broker", "")).strip():
        errors.append("BROKER_NAME_MISSING")
    plan_id = str((orders.get("decision_diagnostics") or {}).get("plan_id", ""))
    if not plan_id or str(evidence.get("plan_id", "")) != plan_id:
        errors.append("BROKER_PLAN_ID_MISMATCH")
    execution_date = str(orders.get("execution_date", ""))[:10]
    if str(evidence.get("execution_date", ""))[:10] != execution_date:
        errors.append("BROKER_EXECUTION_DATE_MISMATCH")
    planned = _planned_orders(orders)
    if not planned:
        errors.append("BROKER_EVIDENCE_WITHOUT_PLANNED_ORDERS")

    fills = evidence.get("fills")
    if not isinstance(fills, list):
        fills = []
        errors.append("BROKER_FILLS_NOT_LIST")
    normalised: List[Dict[str, Any]] = []
    for index, raw in enumerate(fills):
        if not isinstance(raw, dict):
            errors.append(f"BROKER_FILL_{index}_NOT_OBJECT")
            continue
        code = str(raw.get("code", ""))
        side = str(raw.get("side", "")).upper()
        try:
            shares = int(raw.get("shares", 0))
        except (TypeError, ValueError):
            shares = 0
        price = safe_float(raw.get("price"))
        commission = safe_float(raw.get("commission"), -1.0)
        other_fees = safe_float(raw.get("other_fees"), -1.0)
        trade_date = str(raw.get("trade_date", ""))[:10]
        if not code:
            errors.append(f"BROKER_FILL_{index}_CODE_MISSING")
        if side not in {"BUY", "SELL"}:
            errors.append(f"BROKER_FILL_{index}_SIDE_INVALID")
        if shares <= 0:
            errors.append(f"BROKER_FILL_{index}_SHARES_INVALID")
        if price <= 0.0:
            errors.append(f"BROKER_FILL_{index}_PRICE_INVALID")
        if commission < 0.0 or other_fees < 0.0:
            errors.append(f"BROKER_FILL_{index}_FEES_INVALID")
        if trade_date != execution_date:
            errors.append(f"BROKER_FILL_{index}_DATE_MISMATCH")
        normalised.append(
            {
                "code": code,
                "side": side,
                "shares": shares,
                "price": round(price, 6),
                "commission": round(commission, 6),
                "other_fees": round(other_fees, 6),
                "trade_date": trade_date,
            }
        )

    planned_grouped = _aggregate(planned, fill_rows=False)
    fill_grouped = _aggregate(normalised, fill_rows=True)
    raw_outcomes = evidence.get("order_outcomes")
    normalised_outcomes: List[Dict[str, Any]] = []
    outcome_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if raw_outcomes is None:
        if set(planned_grouped) != set(fill_grouped):
            errors.append("BROKER_ORDER_SET_MISMATCH")
        for key in sorted(set(planned_grouped) | set(fill_grouped)):
            planned_shares = int(planned_grouped.get(key, {}).get("shares", 0))
            filled_shares = int(fill_grouped.get(key, {}).get("shares", 0))
            if planned_shares != filled_shares:
                errors.append(f"BROKER_ORDER_QUANTITY_MISMATCH:{key[0]}:{key[1]}")
            outcome = {
                "code": key[0],
                "side": key[1],
                "status": "FILLED",
                "filled_shares": filled_shares,
                "unfilled_shares": 0,
            }
            normalised_outcomes.append(outcome)
            outcome_by_key[key] = outcome
    elif not isinstance(raw_outcomes, list):
        errors.append("BROKER_ORDER_OUTCOMES_NOT_LIST")
    else:
        for index, raw in enumerate(raw_outcomes):
            if not isinstance(raw, Mapping):
                errors.append(f"BROKER_OUTCOME_{index}_NOT_OBJECT")
                continue
            key = (str(raw.get("code", "")), str(raw.get("side", "")).upper())
            status = str(raw.get("status", "")).upper()
            try:
                filled_shares = int(raw.get("filled_shares", -1))
                unfilled_shares = int(raw.get("unfilled_shares", -1))
            except (TypeError, ValueError):
                filled_shares = -1
                unfilled_shares = -1
            if key in outcome_by_key:
                errors.append(f"BROKER_OUTCOME_DUPLICATE:{key[0]}:{key[1]}")
            if key not in planned_grouped:
                errors.append(f"BROKER_OUTCOME_NOT_PLANNED:{key[0]}:{key[1]}")
            planned_shares = int(planned_grouped.get(key, {}).get("shares", 0))
            if filled_shares < 0 or unfilled_shares < 0 or filled_shares + unfilled_shares != planned_shares:
                errors.append(f"BROKER_OUTCOME_QUANTITY_MISMATCH:{key[0]}:{key[1]}")
            expected_status = (
                "UNFILLED"
                if filled_shares == 0
                else ("FILLED" if unfilled_shares == 0 else "PARTIALLY_FILLED")
            )
            if status != expected_status:
                errors.append(f"BROKER_OUTCOME_STATUS_MISMATCH:{key[0]}:{key[1]}")
            actual_filled = int(fill_grouped.get(key, {}).get("shares", 0))
            if actual_filled != filled_shares:
                errors.append(f"BROKER_FILL_OUTCOME_MISMATCH:{key[0]}:{key[1]}")
            outcome = {
                "code": key[0],
                "side": key[1],
                "status": status,
                "filled_shares": filled_shares,
                "unfilled_shares": unfilled_shares,
            }
            normalised_outcomes.append(outcome)
            outcome_by_key[key] = outcome
        if set(outcome_by_key) != set(planned_grouped):
            errors.append("BROKER_OUTCOME_SET_MISMATCH")
        if not set(fill_grouped).issubset(set(planned_grouped)):
            errors.append("BROKER_FILL_SET_NOT_SUBSET_OF_PLAN")

    if errors:
        return list(dict.fromkeys(errors)), {
            "fills": normalised,
            "order_outcomes": normalised_outcomes,
        }

    expected_cost = 0.0
    implementation_shortfall = 0.0
    explicit_fees = 0.0
    broker_gross = 0.0
    comparison_rows: List[Dict[str, Any]] = []
    for key, plan in sorted(planned_grouped.items()):
        outcome = outcome_by_key[key]
        planned_shares = int(plan["shares"])
        shares = int(outcome["filled_shares"])
        planned_price = plan["gross"] / planned_shares
        prorated_model_cost = plan["model_cost"] * shares / planned_shares
        expected_cost += prorated_model_cost
        fill = fill_grouped.get(key, {"gross": 0.0, "commission": 0.0, "other_fees": 0.0})
        actual_price = fill["gross"] / shares if shares > 0 else 0.0
        shortfall = (
            (actual_price - planned_price) * shares
            if key[1] == "BUY"
            else (planned_price - actual_price) * shares
        ) if shares > 0 else 0.0
        fees = fill["commission"] + fill["other_fees"]
        implementation_shortfall += shortfall
        explicit_fees += fees
        broker_gross += fill["gross"]
        comparison_rows.append(
            {
                "code": key[0],
                "side": key[1],
                "shares": shares,
                "planned_shares": planned_shares,
                "unfilled_shares": int(outcome["unfilled_shares"]),
                "fill_status": str(outcome["status"]),
                "planned_price": round(planned_price, 6),
                "actual_price": round(actual_price, 6),
                "explicit_fees": round(fees, 6),
                "implementation_shortfall": round(shortfall, 6),
            }
        )
    actual_cost = explicit_fees + implementation_shortfall
    cost_ratio = actual_cost / expected_cost if expected_cost > 0.0 else None
    excess_cost_bps = (
        (actual_cost - expected_cost) / broker_gross * 10_000.0
        if broker_gross > 0.0
        else None
    )
    total_planned_shares = sum(int(item["shares"]) for item in planned_grouped.values())
    total_filled_shares = sum(int(item["filled_shares"]) for item in normalised_outcomes)
    completion_status = (
        "UNFILLED"
        if total_filled_shares == 0
        else ("COMPLETE" if total_filled_shares == total_planned_shares else "PARTIAL")
    )
    return [], {
        "fills": normalised,
        "order_outcomes": normalised_outcomes,
        "fill_completion_status": completion_status,
        "comparison": comparison_rows,
        "broker_gross": round(broker_gross, 4),
        "expected_model_cost": round(expected_cost, 4),
        "explicit_broker_fees": round(explicit_fees, 4),
        "implementation_shortfall": round(implementation_shortfall, 4),
        "actual_total_cost": round(actual_cost, 4),
        "actual_to_expected_cost_ratio": round(cost_ratio, 8) if cost_ratio is not None else None,
        "excess_cost_bps": round(excess_cost_bps, 6) if excess_cost_bps is not None else None,
    }


def build_execution_feedback(
    orders: Mapping[str, Any],
    *,
    broker_fills_path: Optional[Path] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    planned = _planned_orders(orders)
    evidence_level = MODEL_ESTIMATE_ONLY if planned else NO_ORDERS
    broker_confirmed = False
    rejection_reasons: List[str] = []
    broker_evidence: Dict[str, Any] = {}
    broker_file_sha256 = ""

    if broker_fills_path is not None:
        evidence, broker_file_sha256, load_errors = _load_broker_evidence(
            Path(broker_fills_path)
        )
        rejection_reasons.extend(load_errors)
        if evidence is not None:
            validation_errors, broker_evidence = _validate_broker_evidence(
                evidence, orders
            )
            rejection_reasons.extend(validation_errors)
            if not rejection_reasons:
                evidence_level = BROKER_CONFIRMED
                broker_confirmed = True
                broker_evidence.update(
                    {
                        "broker": str(evidence.get("broker", "")),
                        "account_reference_hash": str(
                            evidence.get("account_reference_hash", "")
                        ),
                        "exported_at": str(evidence.get("exported_at", "")),
                    }
                )
        if rejection_reasons:
            evidence_level = BROKER_EVIDENCE_REJECTED

    diagnostics = dict(orders.get("rotation_source") or {})
    quote_diagnostics = dict(diagnostics.get("quotes") or {})
    decision_diagnostics = dict(orders.get("decision_diagnostics") or {})
    decision_reason_codes = list(
        dict.fromkeys(
            str(item.get("code", "")).strip()
            for item in list(decision_diagnostics.get("reasons") or [])
            if isinstance(item, Mapping) and str(item.get("code", "")).strip()
        )
    )
    feedback: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_level": evidence_level,
        "broker_confirmed": broker_confirmed,
        "plan_id": str(decision_diagnostics.get("plan_id", "")),
        "rebalance_required": bool(
            decision_diagnostics.get("rebalance_required", False)
        ),
        "decision_reason_codes": decision_reason_codes,
        "model_version": str(orders.get("model_version", "")),
        "execution_policy_version": str(orders.get("execution_policy_version", "")),
        "acceptance_policy_version": str(orders.get("acceptance_policy_version", "")),
        "strategy_specification_fingerprint": str(
            orders.get("strategy_specification_fingerprint", "")
        ),
        "data_date": str(orders.get("data_date", ""))[:10],
        "execution_date": str(orders.get("execution_date", ""))[:10],
        "run_date": str(orders.get("run_date", ""))[:10],
        "quote_tradeable": bool(quote_diagnostics.get("tradeable", False)),
        "state_write_allowed": bool(diagnostics.get("state_write_allowed", False)),
        "orders": planned,
        "estimated_execution_cost": round(
            safe_float(orders.get("execution_cost_this_run")), 4
        ),
        "execution_cost_model": dict(orders.get("execution_cost_model") or {}),
        "capacity_summary": dict(orders.get("capacity_summary") or {}),
        "unfilled_order_count": sum(
            int(safe_float(item.get("unfilled_shares")) > 0) for item in planned
        ),
        "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
        "broker_evidence_file_sha256": broker_file_sha256,
        "broker_evidence": broker_evidence,
        "broker_fill_completion_status": str(
            broker_evidence.get("fill_completion_status", "NOT_APPLICABLE")
        ),
        "state_reconciliation_applied": False,
        "state_reconciliation": {},
    }
    feedback["feedback_id"] = _canonical_hash(_feedback_identity_payload(feedback))
    return feedback


def attach_state_reconciliation(
    feedback: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
) -> Dict[str, Any]:
    value = dict(feedback)
    value["state_reconciliation_applied"] = bool(
        reconciliation.get("applied", False)
    )
    value["state_reconciliation"] = dict(reconciliation)
    value["feedback_id"] = _canonical_hash(_feedback_identity_payload(value))
    return value


def save_execution_feedback(
    value: Mapping[str, Any],
    path: Path,
    history_path: Optional[Path] = None,
    *,
    max_history_events: int = 100,
) -> None:
    feedback = dict(value)
    feedback_id = str(feedback.get("feedback_id", ""))
    if len(feedback_id) != 64:
        raise ValueError("execution feedback has no valid fingerprint")
    if history_path is not None:
        history_file = Path(history_path)
        try:
            history = json.loads(history_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = {}
        events = list(history.get("events") or []) if isinstance(history, dict) else []
        known = {
            str(item.get("feedback_id", ""))
            for item in events
            if isinstance(item, dict)
        }
        if feedback_id not in known:
            events.append(feedback)
        events = events[-max(1, int(max_history_events)):]
        atomic_json_save(
            {
                "schema_version": 1,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "event_count": len(events),
                "events": events,
            },
            history_file,
        )
    atomic_json_save(feedback, Path(path))


__all__ = [
    "BROKER_CONFIRMED",
    "BROKER_EVIDENCE_REJECTED",
    "MODEL_ESTIMATE_ONLY",
    "NO_ORDERS",
    "build_virtual_fills_from_plan",
    "build_execution_feedback",
    "attach_state_reconciliation",
    "execution_feedback_identity_valid",
    "save_execution_feedback",
]
