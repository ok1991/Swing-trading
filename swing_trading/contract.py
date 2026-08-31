"""Strict schema V4 contract validation."""

from __future__ import annotations

from typing import Any, Dict, List

from .config import (
    V4_CONFIDENCE_LEVELS,
    V4_DATA_QUALITY_STATES,
    V4_ENTRY_SETUPS,
    V4_ENTRY_STATES,
    V4_SCHEMA_VERSION,
)


V4_REQUIRED_FIELDS = (
    "schema_version", "signal_id", "code", "name", "data_date", "price",
    "data_quality", "trend", "entry", "relative_strength", "risk",
    "market_policy", "calibration",
)


def validate_signal_contract(payload: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if payload.get("schema_version") != V4_SCHEMA_VERSION:
        errors.append(f"payload.schema_version 必须为 {V4_SCHEMA_VERSION}")
    signals = payload.get("signals")
    if not isinstance(signals, list) or not signals:
        return errors + ["signals 列表为空"]

    for index, signal in enumerate(signals):
        code = str(signal.get("code", f"IDX_{index}"))
        for field in V4_REQUIRED_FIELDS:
            if field not in signal:
                errors.append(f"{code}: 缺少字段 '{field}'")
        if signal.get("schema_version") != V4_SCHEMA_VERSION:
            errors.append(f"{code}.schema_version 必须为 {V4_SCHEMA_VERSION}")
        for field in ("data_quality", "trend", "entry", "relative_strength", "risk", "market_policy", "calibration"):
            if not isinstance(signal.get(field), dict):
                errors.append(f"{code}.{field} 必须为 dict")
        quality = signal.get("data_quality") or {}
        entry = signal.get("entry") or {}
        calibration = signal.get("calibration") or {}
        risk = signal.get("risk") or {}
        if quality.get("status") not in V4_DATA_QUALITY_STATES:
            errors.append(f"{code}.data_quality.status 非法")
        if entry.get("state") not in V4_ENTRY_STATES:
            errors.append(f"{code}.entry.state 非法")
        if entry.get("setup") not in V4_ENTRY_SETUPS:
            errors.append(f"{code}.entry.setup 非法")
        if calibration.get("confidence") not in V4_CONFIDENCE_LEVELS:
            errors.append(f"{code}.calibration.confidence 非法")
        if not isinstance(calibration.get("approved"), bool):
            errors.append(f"{code}.calibration.approved 必须为 bool")
        if not isinstance(risk.get("executable"), bool):
            errors.append(f"{code}.risk.executable 必须为 bool")
    return errors
