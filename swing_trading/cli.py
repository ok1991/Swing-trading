"""QingLong and local entrypoint for the approved rotation executor."""

from __future__ import annotations

import argparse
from copy import deepcopy
import logging
import os
from pathlib import Path
from typing import Dict

from .state import atomic_json_save

from .config import (
    LOG_DIR,
    RUNTIME_DIR,
    cache_path,
    ensure_directories,
    execution_feedback_path,
    execution_feedback_history_path,
    execution_feedback_virtual_path,
    execution_feedback_virtual_path,
    execution_plan_archive_dir,
    execution_plan_path,
    live_performance_path,
    local_rotation_source_path,
    pretrade_audit_path,
    report_path,
    ROTATION_SOURCE_PRIORITY,
    state_path,
)
from .engine import TradingEngine
from .broker_reconciliation import apply_broker_reconciliation
from .execution_feedback import (
    attach_state_reconciliation,
    build_virtual_fills_from_plan,
    build_execution_feedback,
    save_execution_feedback,
)
from .execution_plan import (
    build_execution_plan,
    load_execution_plan_artifact,
    save_execution_plan,
)
from .quotes import RealtimeQuote
from .performance import (
    BENCHMARK_CODE,
    record_live_performance,
    save_live_performance,
)
from .reporting import TradeHTMLReporter
from .rotation_source import RotationSource
from .state import StateManager
from .pretrade import validate_pretrade_shadow
from .utils import safe_float


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "swing_trading.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def main_cli() -> None:
    parser = argparse.ArgumentParser(description="已验收行业 ETF 轮动执行引擎")
    parser.add_argument("--rotation", help="本地 etf_rotation_latest.json，仅用于联调")
    parser.add_argument("--run-date", help="YYYY-MM-DD")
    parser.add_argument(
        "--no-realtime",
        action="store_true",
        help="禁用实时行情并强制零交易，仅用于测试",
    )
    parser.add_argument("--publish", action="store_true", help="发布执行报告")
    parser.add_argument(
        "--broker-fills",
        help="结构化券商成交 JSON；校验失败不会用于成本校准",
    )
    parser.add_argument(
        "--feedback-only",
        action="store_true",
        help="只校验迟到的券商成交反馈，不拉行情、不调仓、不写组合状态",
    )
    parser.add_argument(
        "--virtual-confirm",
        action="store_true",
        help="虚拟盘模式：订单生成后按同一执行计划自动生成交割确认",
    )
    parser.add_argument(
        "--execution-plan",
        help="feedback-only使用的历史执行计划；默认读取最新计划",
    )
    parser.add_argument(
        "--apply-broker-state",
        action="store_true",
        help="feedback-only确认成交后，将模型估算状态对账为券商实际现金与成本",
    )
    args = parser.parse_args()
    if args.apply_broker_state and not args.feedback_only:
        parser.error("--apply-broker-state requires --feedback-only")
    if args.virtual_confirm and args.feedback_only:
        parser.error("--virtual-confirm cannot be used with --feedback-only")
    if args.no_realtime and args.publish:
        parser.error("--no-realtime cannot publish production outputs")

    ensure_directories()
    _configure_logging()
    if args.feedback_only:
        if not args.broker_fills:
            parser.error("--feedback-only requires --broker-fills")
        plan_source = Path(args.execution_plan) if args.execution_plan else execution_plan_path()
        plan_artifact = load_execution_plan_artifact(plan_source)
        orders = dict(plan_artifact["orders"])
        feedback = build_execution_feedback(
            orders,
            broker_fills_path=Path(args.broker_fills),
        )
        if args.apply_broker_state:
            state = StateManager.load(state_path())
            reconciliation = apply_broker_reconciliation(
                state,
                orders,
                feedback,
                pre_trade_state=plan_artifact.get("pre_trade_state"),
            )
            StateManager.save(state, state_path())
            if state.get("live_performance"):
                save_live_performance(
                    state["live_performance"], live_performance_path()
                )
            feedback = attach_state_reconciliation(feedback, reconciliation)
        output_feedback = execution_feedback_path()
        save_execution_feedback(
            feedback,
            output_feedback,
            execution_feedback_history_path(),
        )
        if args.publish or os.environ.get("AUTO_GIT_PUSH", "false").lower() == "true":
            from .publish import publish_feedback, publish_live_performance

            publish_feedback(
                source=output_feedback,
                history_source=execution_feedback_history_path(),
            )
            if args.apply_broker_state:
                publish_live_performance(source=live_performance_path())
        logging.info(
            "Delayed broker feedback complete: %s / %s / plan %s",
            feedback["evidence_level"],
            output_feedback,
            feedback["plan_id"],
        )
        return

    state = StateManager.load(state_path())
    pre_trade_state = deepcopy(state)
    snapshot = RotationSource(
        cache_path(),
        local_path=local_rotation_source_path(),
        prefer_local=ROTATION_SOURCE_PRIORITY != "remote_first",
    ).load(args.rotation)
    payload = snapshot.payload
    pretrade_errors = []
    pretrade_path = pretrade_audit_path(str(payload.get("execution_date", "")))
    if not args.no_realtime and snapshot.source != "local" and pretrade_path.is_file():
        pretrade_errors, _ = validate_pretrade_shadow(pretrade_path, payload)
    elif not args.no_realtime and snapshot.source != "local":
        logging.info(
            "Pretrade shadow file not available at %s; relying on rotation contract and realtime quote validation.",
            pretrade_path,
        )

    candidate_prices: Dict[str, float] = {
        str(item.get("code")): safe_float(item.get("price"))
        for item in payload.get("top_candidates", [])
        if item.get("code") and safe_float(item.get("price")) > 0
    }
    quote_codes = set(str(code) for code in (payload.get("target_weights") or {}))
    quote_codes.add(BENCHMARK_CODE)
    quote_codes.update(
        str(holding.get("code"))
        for holding in state.get("holdings", [])
        if holding.get("code")
    )
    quote_diagnostics = {"mode": "NO_REALTIME_TEST_ONLY", "tradeable": False, "errors": []}
    valuation_diagnostics = dict(quote_diagnostics)
    execution_quote_tradeable = True
    valuation_quote_tradeable = True
    if args.no_realtime:
        prices = dict(candidate_prices)
        execution_quote_tradeable = False
        valuation_quote_tradeable = False
        quote_diagnostics["errors"].append("REALTIME_QUOTES_DISABLED")
        valuation_diagnostics["errors"] = list(quote_diagnostics["errors"])
    else:
        quote_snapshot = RealtimeQuote.fetch(quote_codes)
        prices = dict(quote_snapshot.prices)
        quote_diagnostics = quote_snapshot.diagnostics(
            str(payload.get("execution_date", "")),
            args.run_date,
            str(payload.get("data_date", "")),
        )
        valuation_diagnostics = quote_snapshot.valuation_diagnostics(args.run_date)
        execution_quote_tradeable = bool(quote_diagnostics["tradeable"])
        valuation_quote_tradeable = bool(valuation_diagnostics["tradeable"])
        if not valuation_quote_tradeable:
            prices = {}

    combined_diagnostics = snapshot.diagnostics()
    combined_diagnostics["pretrade_shadow"] = {
        "required": not args.no_realtime and snapshot.source != "local" and pretrade_path.is_file(),
        "path": str(pretrade_path),
        "valid": not pretrade_errors,
        "errors": pretrade_errors,
    }
    combined_diagnostics["quotes"] = quote_diagnostics
    combined_diagnostics["valuation_quotes"] = valuation_diagnostics
    combined_diagnostics["state_write_allowed"] = bool(valuation_quote_tradeable)

    engine = TradingEngine(
        state,
        payload,
        prices,
        args.run_date,
        allow_rebalance=(
            snapshot.allow_rebalance
            and execution_quote_tradeable
            and not pretrade_errors
        ),
        allow_valuation=valuation_quote_tradeable,
        source_diagnostics=combined_diagnostics,
    )
    orders = engine.run()
    broker_fills_path = args.broker_fills
    plan_artifact = None
    virtual_feedback = None
    output_report = RUNTIME_DIR / "reports" / "dry_run.html" if args.no_realtime else report_path()
    if not args.no_realtime and valuation_quote_tradeable:
        if orders["buy_orders"] or orders["sell_orders"]:
            if not execution_quote_tradeable:
                raise RuntimeError("orders cannot exist without execution quote authority")
            plan = build_execution_plan(
                orders,
                pre_trade_state=pre_trade_state,
            )
            save_execution_plan(
                plan,
                execution_plan_path(),
                execution_plan_archive_dir(),
            )
            plan_artifact = plan
            if args.virtual_confirm:
                virtual_fills = build_virtual_fills_from_plan(orders)
                virtual_fills_path = execution_feedback_virtual_path()
                atomic_json_save(virtual_fills, virtual_fills_path)
                virtual_feedback = build_execution_feedback(
                    orders,
                    broker_fills_path=virtual_fills_path,
                    generated_at=str(virtual_fills["exported_at"]),
                )
                virtual_feedback = attach_state_reconciliation(
                    virtual_feedback,
                    apply_broker_reconciliation(
                        state,
                        orders,
                        virtual_feedback,
                        pre_trade_state=(
                            plan_artifact.get("pre_trade_state") if plan_artifact else None
                        ),
                    ),
                )
                broker_fills_path = virtual_fills_path
        performance = record_live_performance(
            state,
            total_assets=orders["total_assets"],
            benchmark_price=prices.get(BENCHMARK_CODE, 0.0),
            date=orders["run_date"],
            # Attribute live P&L only to the model actually applied to the
            # portfolio. A downloaded rotation that missed/failed its unique
            # execution session must not acquire observations from old
            # holdings or an unassigned cash book.
            model_version=str(state.get("last_model_version", "")),
            benchmark_quote_evidence=valuation_diagnostics,
        )
        StateManager.save(state, state_path())
        save_live_performance(performance, live_performance_path())
    TradeHTMLReporter.generate(state, orders, prices, output_path=str(output_report))
    # Live/same-host runs always write the public contract paths that ETF-main
    # prefers. --no-realtime is an offline probe and must not poison those
    # production evidence files with non-tradeable SOURCE_BLOCKED payloads.
    output_feedback = (
        RUNTIME_DIR / "reports" / "dry_run_execution_feedback.json"
        if args.no_realtime
        else execution_feedback_path()
    )
    feedback = virtual_feedback if virtual_feedback is not None else build_execution_feedback(
        orders,
        broker_fills_path=broker_fills_path,
    )
    save_execution_feedback(
        feedback,
        output_feedback,
        None if args.no_realtime else execution_feedback_history_path(),
    )

    publish_requested = bool(
        args.publish
        or os.environ.get("AUTO_GIT_PUSH", "false").lower() == "true"
    )
    if args.no_realtime and publish_requested:
        logging.warning(
            "Dry-run publication blocked: --no-realtime outputs remain local only"
        )
    elif publish_requested:
        from .publish import publish_execution_outputs

        publish_execution_outputs(feedback_source=output_feedback)

    logging.info(
        "轮动执行完成: 买入 %d / 卖出 %d / 本次成本 %.2f",
        len(orders["buy_orders"]),
        len(orders["sell_orders"]),
        orders["execution_cost_this_run"],
    )
    logging.info("轮动目标来源: %s", snapshot.source)
    logging.info("状态: %s", "DRY_RUN_NOT_SAVED" if args.no_realtime else state_path())
    logging.info("报告: %s", output_report)
    logging.info("Execution feedback: %s / %s", feedback["evidence_level"], output_feedback)


__all__ = ["main_cli"]
