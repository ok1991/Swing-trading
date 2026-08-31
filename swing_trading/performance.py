"""Live portfolio performance relative to the CSI 300 ETF benchmark."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping

from .state import atomic_json_save
from .utils import safe_float


BENCHMARK_CODE = "510300"
MAX_PERFORMANCE_HISTORY = 520
BROKER_RECONCILED = "BROKER_RECONCILED"
MODEL_ESTIMATE_PENDING = "MODEL_ESTIMATE_PENDING"
NO_EXECUTION_REQUIRED = "NO_EXECUTION_REQUIRED"
INITIAL_STATE = "INITIAL_STATE"
LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"


def _portfolio_state_evidence(state: Mapping[str, Any]) -> Dict[str, str]:
    pending_plan_id = str(
        state.get("pending_broker_confirmation_plan_id", "")
    )
    satisfied_plan_id = str(state.get("last_execution_satisfied_plan_id", ""))
    last_plan_id = str(state.get("last_plan_id", ""))
    reconciliation = dict(state.get("last_broker_reconciliation") or {})
    reconciliation_plan_id = str(reconciliation.get("plan_id", ""))
    reconciliation_id = str(reconciliation.get("reconciliation_id", ""))
    if pending_plan_id:
        evidence = MODEL_ESTIMATE_PENDING
    elif satisfied_plan_id:
        evidence = (
            BROKER_RECONCILED
            if reconciliation_plan_id == satisfied_plan_id
            and str(reconciliation.get("status", ""))
            in {"APPLIED", "ALREADY_APPLIED"}
            else NO_EXECUTION_REQUIRED
        )
    elif not last_plan_id:
        evidence = INITIAL_STATE
    else:
        evidence = LEGACY_UNVERIFIED
    return {
        "portfolio_state_evidence": evidence,
        "pending_broker_confirmation_plan_id": pending_plan_id,
        "last_execution_satisfied_plan_id": satisfied_plan_id,
        "broker_reconciliation_id": (
            reconciliation_id if evidence == BROKER_RECONCILED else ""
        ),
    }


def _period_metrics(records: list[Dict[str, Any]], periods: int) -> Dict[str, Any]:
    if len(records) <= periods:
        return {
            "periods": periods,
            "available": False,
            "strategy_return": None,
            "benchmark_return": None,
            "excess_return": None,
            "relative_return": None,
        }
    current = records[-1]
    prior = records[-periods - 1]
    strategy_return = current["strategy_nav"] / prior["strategy_nav"] - 1.0
    benchmark_return = current["benchmark_nav"] / prior["benchmark_nav"] - 1.0
    relative_return = current["relative_nav"] / prior["relative_nav"] - 1.0
    return {
        "periods": periods,
        "available": True,
        "strategy_return": round(strategy_return, 8),
        "benchmark_return": round(benchmark_return, 8),
        "excess_return": round(strategy_return - benchmark_return, 8),
        "relative_return": round(relative_return, 8),
    }



def build_investor_performance_view(
    live_performance: Mapping[str, Any] | None = None,
    *,
    trade_history: list[Mapping[str, Any]] | None = None,
    min_closed_rounds: int = 3,
) -> Dict[str, Any]:
    """Assemble investor-facing KPIs and chart series from live performance.

    Missing history stays empty on purpose: no fabricated backtest curve.
    """
    payload = dict(live_performance or {})
    history = [dict(item) for item in list(payload.get("history") or []) if item]
    history.sort(key=lambda item: str(item.get("date", "")))
    observation_count = int(payload.get("observation_count") or len(history) or 0)
    has_series = observation_count > 0 and bool(history)

    strategy_returns = [
        safe_float(item.get("strategy_return"), safe_float(item.get("strategy_nav")) - 1.0)
        for item in history
    ]
    max_return = max(strategy_returns) if strategy_returns else None
    chart = []
    for item in history:
        strategy_nav = safe_float(item.get("strategy_nav"))
        benchmark_nav = safe_float(item.get("benchmark_nav"))
        chart.append(
            {
                "date": str(item.get("date", ""))[:10],
                "strategy_return_pct": round((strategy_nav - 1.0) * 100.0, 4)
                if strategy_nav > 0
                else None,
                "benchmark_return_pct": round((benchmark_nav - 1.0) * 100.0, 4)
                if benchmark_nav > 0
                else None,
                "strategy_nav": strategy_nav if strategy_nav > 0 else None,
                "benchmark_nav": benchmark_nav if benchmark_nav > 0 else None,
            }
        )

    evidence = str(payload.get("portfolio_state_evidence") or "")
    trusted_evidence = evidence in {BROKER_RECONCILED, NO_EXECUTION_REQUIRED}
    estimate_only = bool(has_series) and evidence not in {
        "",
        BROKER_RECONCILED,
        NO_EXECUTION_REQUIRED,
        INITIAL_STATE,
    }

    trade_stats = summarize_realized_trade_stats(
        trade_history or [],
        min_closed_rounds=min_closed_rounds,
    )

    return {
        "available": has_series,
        "observation_count": observation_count,
        "benchmark_code": str(payload.get("benchmark_code") or BENCHMARK_CODE),
        "benchmark_label": "沪深300ETF(510300)",
        "data_date": str(payload.get("data_date") or ""),
        "model_version": str(payload.get("model_version") or ""),
        "portfolio_state_evidence": evidence,
        "trusted_evidence": trusted_evidence,
        "estimate_only": estimate_only,
        "sparse_series": has_series and observation_count < 2,
        "strategy_return": payload.get("strategy_return") if has_series else None,
        "benchmark_return": payload.get("benchmark_return") if has_series else None,
        "excess_return": payload.get("excess_return") if has_series else None,
        "relative_return": payload.get("relative_return") if has_series else None,
        "strategy_max_drawdown": payload.get("strategy_max_drawdown") if has_series else None,
        "benchmark_max_drawdown": payload.get("benchmark_max_drawdown") if has_series else None,
        "max_return": round(max_return, 8) if max_return is not None else None,
        "chart": chart,
        "trade_stats": trade_stats,
        "empty_reason": "" if has_series else "等待实盘绩效记录",
    }


def summarize_realized_trade_stats(
    trade_history: list[Mapping[str, Any]] | None = None,
    *,
    min_closed_rounds: int = 3,
) -> Dict[str, Any]:
    """Pair same-code buy/sell lots into realized rounds for win rate / profit factor."""
    lots: Dict[str, list[Dict[str, float]]] = {}
    closed: list[Dict[str, Any]] = []

    for raw in list(trade_history or []):
        side_raw = str(raw.get("side") or "").strip()
        side = side_raw.upper()
        code = str(raw.get("code") or "").strip()
        shares = int(safe_float(raw.get("shares")))
        if not code or shares <= 0:
            continue
        price = safe_float(raw.get("effective_price"), safe_float(raw.get("price")))
        if price <= 0:
            continue
        cost = safe_float(raw.get("total_cost"))
        is_buy = side in {"BUY"} or side_raw in {"买入"}
        is_sell = side in {"SELL"} or side_raw in {"卖出"}
        if is_buy:
            lots.setdefault(code, []).append(
                {
                    "shares": float(shares),
                    "price": price,
                    "cost": cost,
                    "date": str(raw.get("date") or ""),
                    "name": str(raw.get("name") or code),
                }
            )
            continue
        if not is_sell:
            continue
        remaining = float(shares)
        sell_cost_per_share = cost / float(shares) if shares else 0.0
        while remaining > 1e-9 and lots.get(code):
            lot = lots[code][0]
            take = min(remaining, float(lot["shares"]))
            buy_cost_per_share = (
                float(lot["cost"]) / float(lot["shares"]) if lot["shares"] else 0.0
            )
            proceeds = take * price
            basis = take * float(lot["price"])
            allocated_cost = take * (buy_cost_per_share + sell_cost_per_share)
            pnl = proceeds - basis - allocated_cost
            closed.append(
                {
                    "code": code,
                    "name": lot.get("name") or code,
                    "shares": take,
                    "pnl": round(pnl, 4),
                    "buy_date": lot.get("date") or "",
                    "sell_date": str(raw.get("date") or ""),
                }
            )
            lot["shares"] = float(lot["shares"]) - take
            if lot["shares"] <= 1e-9:
                lots[code].pop(0)
            remaining -= take

    pnls = [safe_float(item.get("pnl")) for item in closed]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    closed_count = len(pnls)
    enough = closed_count >= int(min_closed_rounds)
    profit_factor = (gross_profit / gross_loss) if enough and gross_loss > 0 else None
    win_rate = (len(wins) / closed_count) if enough and closed_count else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    return {
        "available": enough,
        "closed_rounds": closed_count,
        "min_closed_rounds": int(min_closed_rounds),
        "win_rate": round(win_rate, 6) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "average_win": round(avg_win, 4) if avg_win is not None else None,
        "average_loss": round(avg_loss, 4) if avg_loss is not None else None,
        "empty_reason": "" if enough else "暂无足够交易样本",
    }


def record_live_performance(
    state: Dict[str, Any],
    *,
    total_assets: float,
    benchmark_price: float,
    date: str,
    model_version: str,
    benchmark_quote_evidence: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    assets = safe_float(total_assets)
    benchmark = safe_float(benchmark_price)
    record_date = str(date)[:10]
    if assets <= 0.0 or benchmark <= 0.0 or len(record_date) != 10:
        raise ValueError("live performance requires positive assets, benchmark, and date")
    baseline = dict(state.get("performance_baseline") or {})
    if not baseline:
        baseline = {
            "date": record_date,
            "strategy_assets": assets,
            "benchmark_price": benchmark,
            "source": "FIRST_VALID_MARK_TO_MARKET",
        }
    if safe_float(baseline.get("strategy_assets")) <= 0.0 or safe_float(
        baseline.get("benchmark_price")
    ) <= 0.0:
        raise ValueError("live performance baseline is invalid")

    raw_records = [
        dict(item)
        for item in list(state.get("performance_history") or [])
        if str(item.get("date", ""))[:10] != record_date
    ]
    raw_records.append(
        {
            "date": record_date,
            "total_assets": round(assets, 4),
            "benchmark_price": round(benchmark, 6),
            "model_version": str(model_version),
            "benchmark_quote_source": str(
                (benchmark_quote_evidence or {}).get("source", "UNVERIFIED")
            ),
            "benchmark_quote_mode": str(
                (benchmark_quote_evidence or {}).get("mode", "UNVERIFIED")
            ),
            "benchmark_quote_date": str(
                ((benchmark_quote_evidence or {}).get("quote_dates") or {}).get(
                    BENCHMARK_CODE, ""
                )
            )[:10],
            "benchmark_quote_time": str(
                ((benchmark_quote_evidence or {}).get("quote_times") or {}).get(
                    BENCHMARK_CODE, ""
                )
            )[:8],
            "benchmark_quote_fetched_at": str(
                (benchmark_quote_evidence or {}).get("fetched_at", "")
            ),
            "benchmark_quote_validated_at": str(
                (benchmark_quote_evidence or {}).get("validated_at", "")
            ),
            "benchmark_quote_tradeable": bool(
                (benchmark_quote_evidence or {}).get("tradeable", False)
            ),
            **_portfolio_state_evidence(state),
        }
    )
    raw_records.sort(key=lambda item: str(item.get("date", "")))
    raw_records = raw_records[-MAX_PERFORMANCE_HISTORY:]

    strategy_peak = 0.0
    benchmark_peak = 0.0
    relative_peak = 0.0
    strategy_max_drawdown = 0.0
    benchmark_max_drawdown = 0.0
    relative_max_drawdown = 0.0
    records: list[Dict[str, Any]] = []
    for item in raw_records:
        strategy_nav = safe_float(item.get("total_assets")) / safe_float(
            baseline["strategy_assets"]
        )
        benchmark_nav = safe_float(item.get("benchmark_price")) / safe_float(
            baseline["benchmark_price"]
        )
        relative_nav = strategy_nav / benchmark_nav if benchmark_nav > 0.0 else 0.0
        strategy_peak = max(strategy_peak, strategy_nav)
        benchmark_peak = max(benchmark_peak, benchmark_nav)
        relative_peak = max(relative_peak, relative_nav)
        strategy_drawdown = strategy_nav / strategy_peak - 1.0 if strategy_peak else 0.0
        benchmark_drawdown = benchmark_nav / benchmark_peak - 1.0 if benchmark_peak else 0.0
        relative_drawdown = relative_nav / relative_peak - 1.0 if relative_peak else 0.0
        strategy_max_drawdown = min(strategy_max_drawdown, strategy_drawdown)
        benchmark_max_drawdown = min(benchmark_max_drawdown, benchmark_drawdown)
        relative_max_drawdown = min(relative_max_drawdown, relative_drawdown)
        records.append(
            {
                **item,
                "strategy_nav": round(strategy_nav, 8),
                "benchmark_nav": round(benchmark_nav, 8),
                "relative_nav": round(relative_nav, 8),
                "strategy_return": round(strategy_nav - 1.0, 8),
                "benchmark_return": round(benchmark_nav - 1.0, 8),
                "excess_return": round(strategy_nav - benchmark_nav, 8),
                "relative_return": round(relative_nav - 1.0, 8),
                "strategy_drawdown": round(strategy_drawdown, 8),
                "benchmark_drawdown": round(benchmark_drawdown, 8),
                "relative_drawdown": round(relative_drawdown, 8),
            }
        )

    current = records[-1]
    artifact = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "benchmark_code": BENCHMARK_CODE,
        "baseline": baseline,
        "observation_count": len(records),
        "data_date": current["date"],
        "model_version": current["model_version"],
        "portfolio_state_evidence": current["portfolio_state_evidence"],
        "pending_broker_confirmation_plan_id": current[
            "pending_broker_confirmation_plan_id"
        ],
        "last_execution_satisfied_plan_id": current[
            "last_execution_satisfied_plan_id"
        ],
        "broker_reconciliation_id": current["broker_reconciliation_id"],
        "benchmark_quote_source": current["benchmark_quote_source"],
        "benchmark_quote_mode": current["benchmark_quote_mode"],
        "benchmark_quote_date": current["benchmark_quote_date"],
        "benchmark_quote_time": current["benchmark_quote_time"],
        "benchmark_quote_fetched_at": current["benchmark_quote_fetched_at"],
        "benchmark_quote_validated_at": current["benchmark_quote_validated_at"],
        "benchmark_quote_tradeable": current["benchmark_quote_tradeable"],
        "total_assets": current["total_assets"],
        "strategy_nav": current["strategy_nav"],
        "benchmark_nav": current["benchmark_nav"],
        "relative_nav": current["relative_nav"],
        "strategy_return": current["strategy_return"],
        "benchmark_return": current["benchmark_return"],
        "excess_return": current["excess_return"],
        "relative_return": current["relative_return"],
        "strategy_max_drawdown": round(strategy_max_drawdown, 8),
        "benchmark_max_drawdown": round(benchmark_max_drawdown, 8),
        "relative_max_drawdown": round(relative_max_drawdown, 8),
        "rolling_20": _period_metrics(records, 20),
        "rolling_60": _period_metrics(records, 60),
        "history": records,
    }
    artifact["performance_id"] = hashlib.sha256(
        json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    state["performance_baseline"] = baseline
    state["performance_history"] = raw_records
    state["live_performance"] = artifact
    return artifact


def reconcile_latest_performance_cash(
    state: Dict[str, Any],
    cash_adjustment: float,
) -> Dict[str, Any]:
    history = list(state.get("performance_history") or [])
    if not history:
        return {}
    latest = max(history, key=lambda item: str(item.get("date", "")))
    return record_live_performance(
        state,
        total_assets=safe_float(latest.get("total_assets")) + safe_float(cash_adjustment),
        benchmark_price=safe_float(latest.get("benchmark_price")),
        date=str(latest.get("date", "")),
        model_version=str(latest.get("model_version", "")),
        benchmark_quote_evidence={
            "source": latest.get("benchmark_quote_source", "UNVERIFIED"),
            "mode": latest.get("benchmark_quote_mode", "UNVERIFIED"),
            "quote_dates": {
                BENCHMARK_CODE: latest.get("benchmark_quote_date", "")
            },
            "quote_times": {
                BENCHMARK_CODE: latest.get("benchmark_quote_time", "")
            },
            "fetched_at": latest.get("benchmark_quote_fetched_at", ""),
            "validated_at": latest.get("benchmark_quote_validated_at", ""),
            "tradeable": latest.get("benchmark_quote_tradeable") is True,
        },
    )


def save_live_performance(value: Mapping[str, Any], path: Path) -> None:
    atomic_json_save(dict(value), Path(path))


__all__ = [
    "BENCHMARK_CODE",
    "BROKER_RECONCILED",
    "INITIAL_STATE",
    "LEGACY_UNVERIFIED",
    "MODEL_ESTIMATE_PENDING",
    "NO_EXECUTION_REQUIRED",
    "build_investor_performance_view",
    "record_live_performance",
    "reconcile_latest_performance_cash",
    "save_live_performance",
    "summarize_realized_trade_stats",
]
