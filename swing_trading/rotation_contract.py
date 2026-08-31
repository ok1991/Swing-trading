"""Strict contract for the approved rotation target portfolio."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from .config import (
    ROTATION_ACCEPTANCE_POLICY_VERSION,
    ROTATION_EXECUTION_POLICY_VERSION,
    ROTATION_SCHEMA_VERSION,
)
from .costs import DEFAULT_COST_MODEL


def validate_rotation_contract(payload: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if payload.get("schema_version") != ROTATION_SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {ROTATION_SCHEMA_VERSION}")
    if payload.get("approved") is not True:
        errors.append("轮动模型未通过验收")
    if not str(payload.get("model_version", "")).strip():
        errors.append("缺少 model_version")
    if payload.get("execution_policy_version") != ROTATION_EXECUTION_POLICY_VERSION:
        errors.append("执行政策版本与当前引擎不一致")
    if payload.get("acceptance_policy_version") != ROTATION_ACCEPTANCE_POLICY_VERSION:
        errors.append("验收政策版本与当前研究授权不一致")
    expected_exposure_authority = (
        "risk_control_fail_closed"
        if payload.get("risk_control_only") is True
        else "v4_market_policy"
    )
    if payload.get("exposure_authority") != expected_exposure_authority:
        errors.append("仓位权威与执行目标类型不一致")
    if "risk_budget_profile" in payload:
        errors.append("禁止 rotation 层覆盖市场风险预算")
    if payload.get("risk_control_only") is not True:
        specification_fingerprint = str(
            payload.get("strategy_specification_fingerprint", "")
        ).strip().lower()
        if not (
            len(specification_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in specification_fingerprint)
        ):
            errors.append("策略规格指纹必须为64位小写十六进制")
        elif not str(payload.get("model_version", "")).endswith(
            specification_fingerprint[:8]
        ):
            errors.append("model_version 与策略规格指纹不一致")
    try:
        data_date = datetime.strptime(str(payload.get("data_date", ""))[:10], "%Y-%m-%d")
        execution_date = datetime.strptime(
            str(payload.get("execution_date", ""))[:10], "%Y-%m-%d"
        )
    except (TypeError, ValueError):
        errors.append("data_date 和 execution_date 必须为 YYYY-MM-DD")
    else:
        if execution_date <= data_date:
            errors.append("execution_date 必须晚于 data_date")

    try:
        max_exposure = float(payload.get("max_exposure_ratio"))
        cash_weight = float(payload.get("cash_weight"))
    except (TypeError, ValueError):
        max_exposure = -1.0
        cash_weight = -1.0
        errors.append("缺少有效的 max_exposure_ratio/cash_weight")
    else:
        if not 0.0 <= max_exposure <= 1.0:
            errors.append("max_exposure_ratio 必须在 [0, 1] 内")
        if not 0.0 <= cash_weight <= 1.0:
            errors.append("cash_weight 必须在 [0, 1] 内")
        if abs(max_exposure + cash_weight - 1.0) > 1e-4:
            errors.append("max_exposure_ratio 与 cash_weight 合计必须为 1")

    try:
        capacity_reference_capital = float(payload.get("capacity_reference_capital"))
    except (TypeError, ValueError):
        errors.append("capacity_reference_capital must be numeric")
    else:
        if abs(capacity_reference_capital - 10_000.0) > 0.01:
            errors.append("capacity_reference_capital must match the approved backtest")

    weights = payload.get("target_weights")
    if not isinstance(weights, dict):
        errors.append("target_weights 必须为 object")
    else:
        total = 0.0
        for code, raw_weight in weights.items():
            if not (str(code).isdigit() and len(str(code)) == 6):
                errors.append(f"非法 ETF 代码: {code}")
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError):
                errors.append(f"{code}.weight 必须为数值")
                continue
            if weight <= 0.0 or weight > 1.0:
                errors.append(f"{code}.weight 必须在 (0, 1] 内")
            total += weight
        if max_exposure > 0.0 and not weights:
            errors.append("有风险预算时 target_weights 不能为空")
        if abs(total - max(max_exposure, 0.0)) > 1e-4:
            errors.append(
                f"target_weights 合计必须等于 max_exposure_ratio，当前为 {total:.6f}/{max_exposure:.6f}"
            )

    liquidity = payload.get("execution_liquidity")
    if not isinstance(liquidity, dict):
        errors.append("execution_liquidity 必须为 object")
    elif isinstance(weights, dict):
        for code in weights:
            item = liquidity.get(str(code))
            if not isinstance(item, dict):
                errors.append(f"目标 {code} 缺少执行流动性")
                continue
            try:
                average_amount = float(item.get("average_daily_amount_20"))
            except (TypeError, ValueError):
                average_amount = 0.0
            if average_amount <= 0.0:
                errors.append(f"目标 {code} 的20日平均成交额必须为正数")
            try:
                max_new_risk_amount = float(item.get("max_new_risk_amount"))
                max_participation_rate = float(item.get("max_participation_rate"))
            except (TypeError, ValueError):
                errors.append(f"target {code} capacity headroom must be numeric")
            else:
                expected_rate = float(DEFAULT_COST_MODEL.max_participation_rate)
                expected_amount = average_amount * expected_rate
                if abs(max_participation_rate - expected_rate) > 1e-12:
                    errors.append(f"target {code} capacity participation rate mismatch")
                if abs(max_new_risk_amount - expected_amount) > max(0.01, expected_amount * 1e-10):
                    errors.append(f"target {code} capacity headroom mismatch")
                required_amount = capacity_reference_capital * float(weights.get(code, 0.0))
                if max_new_risk_amount + 0.01 < required_amount:
                    errors.append(f"target {code} capacity headroom cannot carry target weight")
            if str(item.get("as_of_date", ""))[:10] != str(payload.get("data_date", ""))[:10]:
                errors.append(f"目标 {code} 的流动性日期与 data_date 不一致")

    market_policy = payload.get("market_policy")
    if not isinstance(market_policy, dict):
        errors.append("market_policy 必须为 object")
    else:
        if "source_max_exposure_ratio" in market_policy:
            errors.append("market_policy 不得携带被覆盖的原始仓位")
        try:
            policy_exposure = float(market_policy.get("max_exposure_ratio"))
        except (TypeError, ValueError):
            errors.append("market_policy 缺少有效的 max_exposure_ratio")
        else:
            if abs(policy_exposure - max(max_exposure, 0.0)) > 1e-4:
                errors.append("market_policy 与目标风险预算不一致")
            if (
                str(market_policy.get("entry_permission", "")) == "BLOCKED"
                and policy_exposure > 1e-4
            ):
                errors.append("BLOCKED 市场策略的仓位必须为 0")

    sleeves = payload.get("sleeves")
    if not isinstance(sleeves, list) or len(sleeves) != 2:
        errors.append("sleeves 必须包含两个错开轮动袖套")

    expected_cost = {
        **DEFAULT_COST_MODEL.to_dict(),
        "transfer_fee_rate": 0.0,
        "stamp_duty_sell_rate": 0.0,
    }
    walk_forward_metrics = payload.get("walk_forward_metrics") or {}
    capacity_fields = {
        "capacity_truncation_count": int,
        "requested_buy_value": float,
        "executed_buy_value": float,
        "capacity_truncated_buy_value": float,
        "unfilled_buy_value": float,
        "buy_fill_ratio": float,
        "capacity_fill_ratio": float,
    }
    capacity_values: Dict[str, float] = {}
    for field, value_type in capacity_fields.items():
        try:
            value = value_type(walk_forward_metrics.get(field))
        except (TypeError, ValueError):
            errors.append(f"walk_forward_metrics.{field} must be numeric")
            continue
        capacity_values[field] = float(value)
        if value < 0.0:
            errors.append(f"walk_forward_metrics.{field} must be non-negative")
    for field in ("buy_fill_ratio", "capacity_fill_ratio"):
        if field in capacity_values and capacity_values[field] > 1.0:
            errors.append(f"walk_forward_metrics.{field} must be at most 1")
    if (
        "requested_buy_value" in capacity_values
        and "executed_buy_value" in capacity_values
        and capacity_values["executed_buy_value"] > capacity_values["requested_buy_value"] + 0.01
    ):
        errors.append("executed buy value cannot exceed requested buy value")
    if (
        "capacity_truncated_buy_value" in capacity_values
        and "unfilled_buy_value" in capacity_values
        and capacity_values["capacity_truncated_buy_value"] > capacity_values["unfilled_buy_value"] + 0.01
    ):
        errors.append("capacity-truncated value cannot exceed total unfilled value")
    cost_model = walk_forward_metrics.get("cost_model") or {}
    for field, expected in expected_cost.items():
        try:
            recorded = float(cost_model.get(field))
        except (TypeError, ValueError):
            errors.append(f"验收成本模型缺少有效字段: {field}")
            continue
        tolerance = 0.0 if field == "lot_size" else 1e-12
        if abs(recorded - float(expected)) > tolerance:
            errors.append(
                f"验收成本 {field}={recorded:g} 与实盘 {float(expected):g} 不一致"
            )
    return errors


__all__ = ["validate_rotation_contract"]
