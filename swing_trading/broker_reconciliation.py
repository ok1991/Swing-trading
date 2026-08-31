"""Reconcile model-estimated portfolio state to validated broker fills."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Dict, Mapping, Tuple

from .utils import safe_float
from .execution_feedback import execution_feedback_identity_valid
from .performance import reconcile_latest_performance_cash


def _reconciliation_id(feedback: Mapping[str, Any]) -> str:
    plan_id = str(feedback.get("plan_id", ""))
    evidence_digest = str(feedback.get("broker_evidence_file_sha256", ""))
    return hashlib.sha256(f"{plan_id}|{evidence_digest}".encode("utf-8")).hexdigest()


def _group_fills(feedback: Mapping[str, Any]) -> Dict[Tuple[str, str], Dict[str, float]]:
    grouped: Dict[Tuple[str, str], Dict[str, float]] = {}
    for fill in (feedback.get("broker_evidence") or {}).get("fills", []) or []:
        key = (str(fill.get("code", "")), str(fill.get("side", "")).upper())
        shares = int(fill.get("shares", 0) or 0)
        target = grouped.setdefault(
            key,
            {"shares": 0.0, "gross": 0.0, "fees": 0.0},
        )
        target["shares"] += shares
        target["gross"] += shares * safe_float(fill.get("price"))
        target["fees"] += safe_float(fill.get("commission")) + safe_float(
            fill.get("other_fees")
        )
    return grouped


def _rebuild_from_pre_trade_state(
    state: Dict[str, Any],
    orders: Mapping[str, Any],
    feedback: Mapping[str, Any],
    pre_trade_state: Mapping[str, Any],
    reconciliation_id: str,
) -> Dict[str, Any]:
    working = deepcopy(dict(pre_trade_state))
    plan_id = str(feedback.get("plan_id", ""))
    fills = _group_fills(feedback)
    outcomes = {
        (str(item.get("code", "")), str(item.get("side", "")).upper()): dict(item)
        for item in (feedback.get("broker_evidence") or {}).get("order_outcomes", []) or []
    }
    planned = list(orders.get("buy_orders") or []) + list(orders.get("sell_orders") or [])
    if set(outcomes) != {
        (str(item.get("code", "")), str(item.get("side", "")).upper())
        for item in planned
    }:
        raise ValueError("partial broker reconciliation outcomes do not match plan")
    holdings = {
        str(item.get("code", "")): dict(item)
        for item in working.get("holdings", [])
        if item.get("code")
    }
    history = list(working.get("trade_history", []) or [])
    total_actual_cost = 0.0
    reconciliation_rows = []
    order_prices: Dict[str, float] = {}
    for order in planned:
        code = str(order.get("code", ""))
        side = str(order.get("side", "")).upper()
        key = (code, side)
        outcome = outcomes[key]
        planned_shares = int(order.get("shares", 0) or 0)
        filled_shares = int(outcome.get("filled_shares", 0) or 0)
        unfilled_shares = int(outcome.get("unfilled_shares", 0) or 0)
        if filled_shares + unfilled_shares != planned_shares:
            raise ValueError(f"partial reconciliation quantity mismatch for {code} {side}")
        fill = fills.get(key, {"shares": 0.0, "gross": 0.0, "fees": 0.0})
        if int(fill["shares"]) != filled_shares:
            raise ValueError(f"partial reconciliation fills mismatch for {code} {side}")
        planned_price = safe_float(order.get("gross")) / max(planned_shares, 1)
        actual_gross = float(fill["gross"])
        explicit_fees = float(fill["fees"])
        actual_price = actual_gross / filled_shares if filled_shares > 0 else 0.0
        shortfall = (
            (actual_price - planned_price) * filled_shares
            if side == "BUY"
            else (planned_price - actual_price) * filled_shares
        ) if filled_shares > 0 else 0.0
        actual_cost = explicit_fees + shortfall
        total_actual_cost += actual_cost
        order_prices[code] = safe_float(order.get("price"), planned_price)
        if filled_shares > 0 and side == "SELL":
            holding = holdings.get(code)
            if holding is None or int(holding.get("shares", 0) or 0) < filled_shares:
                raise ValueError(f"pre-trade holding is insufficient for partial sell: {code}")
            holding["shares"] = int(holding["shares"]) - filled_shares
            working["cash"] = round(
                safe_float(working.get("cash")) + actual_gross - explicit_fees,
                4,
            )
            if int(holding["shares"]) == 0:
                holdings.pop(code, None)
        elif filled_shares > 0 and side == "BUY":
            holding = holdings.get(code, {})
            prior_shares = int(holding.get("shares", 0) or 0)
            prior_basis = safe_float(holding.get("buy_price")) * prior_shares
            total_shares = prior_shares + filled_shares
            holding.update(
                {
                    "code": code,
                    "name": str(order.get("name", code)),
                    "shares": total_shares,
                    "buy_price": round(
                        (prior_basis + actual_gross + explicit_fees) / total_shares,
                        6,
                    ),
                    "buy_date": holding.get("buy_date")
                    or str(feedback.get("execution_date", ""))[:10],
                    "source": "broker_reconciled_rotation",
                    "model_version": str(orders.get("model_version", "")),
                }
            )
            holdings[code] = holding
            working["cash"] = round(
                safe_float(working.get("cash")) - actual_gross - explicit_fees,
                4,
            )
        if filled_shares > 0:
            history.append(
                {
                    **dict(order),
                    "shares": filled_shares,
                    "price": round(actual_price, 6),
                    "gross": round(actual_gross, 4),
                    "total_cost": round(actual_cost, 4),
                    "date": str(feedback.get("execution_date", ""))[:10],
                    "broker_confirmed": True,
                    "broker_reconciliation_id": reconciliation_id,
                    "broker_explicit_fees": round(explicit_fees, 4),
                    "planned_shares": planned_shares,
                    "unfilled_shares": unfilled_shares,
                    "fill_status": str(outcome.get("status", "")),
                }
            )
        reconciliation_rows.append(
            {
                "code": code,
                "side": side,
                "planned_shares": planned_shares,
                "filled_shares": filled_shares,
                "unfilled_shares": unfilled_shares,
                "fill_status": str(outcome.get("status", "")),
                "actual_price": round(actual_price, 6),
                "actual_explicit_fees": round(explicit_fees, 4),
            }
        )
    working["holdings"] = list(holdings.values())
    working["trade_history"] = history
    working["last_plan_id"] = plan_id
    working["last_model_version"] = str(orders.get("model_version", ""))
    working["pending_broker_confirmation_plan_id"] = ""
    working["last_execution_satisfied_plan_id"] = plan_id
    working["last_run"] = str(state.get("last_run", working.get("last_run", "")))
    working["peak_capital"] = state.get("peak_capital", working.get("peak_capital"))
    working["max_drawdown"] = state.get("max_drawdown", working.get("max_drawdown"))
    working["cumulative_execution_cost"] = round(
        safe_float(pre_trade_state.get("cumulative_execution_cost")) + total_actual_cost,
        4,
    )
    for field in ("performance_baseline", "performance_history", "live_performance"):
        working[field] = deepcopy(state.get(field, working.get(field)))
    current_holdings = {
        str(item.get("code", "")): int(item.get("shares", 0) or 0)
        for item in state.get("holdings", [])
    }
    actual_holdings = {
        str(item.get("code", "")): int(item.get("shares", 0) or 0)
        for item in working.get("holdings", [])
    }
    asset_adjustment = safe_float(working.get("cash")) - safe_float(state.get("cash"))
    for code, price in order_prices.items():
        asset_adjustment += (
            actual_holdings.get(code, 0) - current_holdings.get(code, 0)
        ) * price
    working["last_broker_reconciliation"] = {
        "status": "APPLIED",
        "reconciliation_id": reconciliation_id,
        "plan_id": plan_id,
    }
    performance = reconcile_latest_performance_cash(working, asset_adjustment)
    reconciled = [str(value) for value in state.get("broker_reconciled_feedback_ids", [])]
    reconciled.append(reconciliation_id)
    working["broker_reconciled_feedback_ids"] = reconciled[-100:]
    summary = {
        "status": "APPLIED",
        "applied": True,
        "reconciliation_id": reconciliation_id,
        "plan_id": plan_id,
        "execution_date": str(feedback.get("execution_date", ""))[:10],
        "fill_completion_status": str(
            feedback.get("broker_fill_completion_status", "")
        ),
        "cash_adjustment": round(
            safe_float(working.get("cash")) - safe_float(state.get("cash")), 4
        ),
        "expected_model_cost": round(
            safe_float(
                (feedback.get("broker_evidence") or {}).get(
                    "expected_model_cost", feedback.get("estimated_execution_cost")
                )
            ),
            4,
        ),
        "actual_total_cost": round(total_actual_cost, 4),
        "orders": reconciliation_rows,
        "performance_reconciled": bool(performance),
    }
    working["last_broker_reconciliation"] = summary
    state.clear()
    state.update(working)
    return summary


def apply_broker_reconciliation(
    state: Dict[str, Any],
    orders: Mapping[str, Any],
    feedback: Mapping[str, Any],
    pre_trade_state: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    if not execution_feedback_identity_valid(feedback):
        raise ValueError("execution feedback fingerprint is invalid")
    if feedback.get("evidence_level") != "BROKER_CONFIRMED" or feedback.get(
        "broker_confirmed"
    ) is not True:
        raise ValueError("only broker-confirmed feedback can reconcile portfolio state")
    plan_id = str((orders.get("decision_diagnostics") or {}).get("plan_id", ""))
    if not plan_id or plan_id != str(feedback.get("plan_id", "")):
        raise ValueError("broker reconciliation plan does not match execution feedback")
    if str(state.get("last_plan_id", "")) != plan_id:
        raise ValueError("portfolio state is not positioned at the broker feedback plan")
    reconciliation_id = _reconciliation_id(feedback)
    reconciled = [str(value) for value in state.get("broker_reconciled_feedback_ids", [])]
    if reconciliation_id in reconciled:
        return {
            "status": "ALREADY_APPLIED",
            "applied": False,
            "reconciliation_id": reconciliation_id,
            "plan_id": plan_id,
        }
    if str(state.get("pending_broker_confirmation_plan_id", "")) != plan_id:
        raise ValueError("portfolio state is not awaiting this broker feedback plan")
    completion_status = str(
        feedback.get("broker_fill_completion_status", "COMPLETE")
    )
    if completion_status in {"PARTIAL", "UNFILLED"}:
        if not isinstance(pre_trade_state, Mapping):
            raise ValueError("partial broker reconciliation requires a V2 pre-trade state")
        return _rebuild_from_pre_trade_state(
            state,
            orders,
            feedback,
            pre_trade_state,
            reconciliation_id,
        )

    planned = list(orders.get("buy_orders") or []) + list(orders.get("sell_orders") or [])
    fills = _group_fills(feedback)
    planned_keys = {(str(item.get("code", "")), str(item.get("side", "")).upper()) for item in planned}
    if planned_keys != set(fills):
        raise ValueError("broker reconciliation fill set does not match planned orders")

    working = deepcopy(state)
    holdings = {
        str(item.get("code", "")): item
        for item in working.get("holdings", [])
        if item.get("code")
    }
    planned_cash_delta = 0.0
    actual_cash_delta = 0.0
    buy_basis_adjustments: Dict[str, float] = {}
    order_reconciliations = []
    for order in planned:
        code = str(order.get("code", ""))
        side = str(order.get("side", "")).upper()
        shares = int(order.get("shares", 0) or 0)
        fill = fills[(code, side)]
        if int(fill["shares"]) != shares or shares <= 0:
            raise ValueError(f"broker reconciliation quantity mismatch for {code} {side}")
        planned_gross = safe_float(order.get("gross"))
        planned_cost = safe_float(order.get("total_cost"))
        actual_gross = float(fill["gross"])
        actual_fees = float(fill["fees"])
        if side == "BUY":
            planned_cash_delta -= planned_gross + planned_cost
            actual_cash_delta -= actual_gross + actual_fees
            buy_basis_adjustments[code] = buy_basis_adjustments.get(code, 0.0) + (
                actual_gross + actual_fees - planned_gross - planned_cost
            )
        elif side == "SELL":
            planned_cash_delta += planned_gross - planned_cost
            actual_cash_delta += actual_gross - actual_fees
        else:
            raise ValueError("broker reconciliation side is invalid")
        order_reconciliations.append(
            {
                "code": code,
                "side": side,
                "shares": shares,
                "planned_price": round(planned_gross / shares, 6),
                "actual_price": round(actual_gross / shares, 6),
                "planned_cost": round(planned_cost, 4),
                "actual_explicit_fees": round(actual_fees, 4),
            }
        )

    for code, adjustment in buy_basis_adjustments.items():
        holding = holdings.get(code)
        if holding is None or int(holding.get("shares", 0) or 0) <= 0:
            raise ValueError(f"post-plan holding missing for broker buy reconciliation: {code}")
        holding_shares = int(holding["shares"])
        current_basis = safe_float(holding.get("buy_price")) * holding_shares
        holding["buy_price"] = round(
            (current_basis + adjustment) / holding_shares,
            6,
        )

    cash_adjustment = actual_cash_delta - planned_cash_delta
    expected_cost = safe_float(
        (feedback.get("broker_evidence") or {}).get(
            "expected_model_cost", feedback.get("estimated_execution_cost")
        )
    )
    actual_cost = safe_float(
        (feedback.get("broker_evidence") or {}).get("actual_total_cost")
    )
    working["cash"] = round(safe_float(working.get("cash")) + cash_adjustment, 4)
    working["cumulative_execution_cost"] = round(
        safe_float(working.get("cumulative_execution_cost"))
        + actual_cost
        - expected_cost,
        4,
    )

    history = working.get("trade_history", [])
    execution_date = str(feedback.get("execution_date", ""))[:10]
    for item in order_reconciliations:
        match = next(
            (
                row
                for row in history
                if str(row.get("date", ""))[:10] == execution_date
                and str(row.get("code", "")) == item["code"]
                and str(row.get("side", "")).upper() == item["side"]
                and int(row.get("shares", 0) or 0) == item["shares"]
                and not row.get("broker_confirmed")
            ),
            None,
        )
        if match is not None:
            match.update(
                {
                    "broker_confirmed": True,
                    "broker_reconciliation_id": reconciliation_id,
                    "broker_actual_price": item["actual_price"],
                    "broker_explicit_fees": item["actual_explicit_fees"],
                }
            )

    reconciled.append(reconciliation_id)
    working["broker_reconciled_feedback_ids"] = reconciled[-100:]
    summary = {
        "status": "APPLIED",
        "applied": True,
        "reconciliation_id": reconciliation_id,
        "plan_id": plan_id,
        "execution_date": execution_date,
        "cash_adjustment": round(cash_adjustment, 4),
        "expected_model_cost": round(expected_cost, 4),
        "actual_total_cost": round(actual_cost, 4),
        "cumulative_cost_adjustment": round(actual_cost - expected_cost, 4),
        "orders": order_reconciliations,
    }
    working["pending_broker_confirmation_plan_id"] = ""
    working["last_execution_satisfied_plan_id"] = plan_id
    working["last_broker_reconciliation"] = {
        "status": "APPLIED",
        "reconciliation_id": reconciliation_id,
        "plan_id": plan_id,
    }
    performance = reconcile_latest_performance_cash(working, cash_adjustment)
    summary["performance_reconciled"] = bool(performance)
    if performance:
        summary["reconciled_total_assets"] = performance.get("total_assets")
        summary["reconciled_relative_nav"] = performance.get("relative_nav")
    working["last_broker_reconciliation"] = summary
    state.clear()
    state.update(working)
    return summary


__all__ = ["apply_broker_reconciliation"]
