"""Reproducible, non-authorising pretrade shadow for an approved rotation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .engine import TradingEngine
from .rotation_contract import validate_rotation_contract
from .state import StateManager, atomic_json_save


POLICY_VERSION = "rotation-pretrade-shadow-v1"


def _read_object(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_pretrade_shadow(
    audit_path: Path,
    rotation: Mapping[str, Any],
) -> tuple[list[str], Dict[str, Any]]:
    errors: list[str] = []
    try:
        audit = _read_object(audit_path)
    except Exception as error:
        return ["PRETRADE_AUDIT_UNAVAILABLE:" + str(error)], {}
    audit_rotation = audit.get("rotation") or {}
    if audit.get("status") != "READY_FOR_EXECUTION_DATE_QUOTE_REVALIDATION":
        errors.append("PRETRADE_STATUS_NOT_READY")
    if audit.get("shadow_only") is not True:
        errors.append("PRETRADE_SHADOW_ONLY_NOT_TRUE")
    if audit.get("order_submission_allowed") is not False:
        errors.append("PRETRADE_ORDER_SUBMISSION_NOT_DISABLED")
    if audit.get("state_persisted") is not False:
        errors.append("PRETRADE_STATE_PERSISTED")
    if audit.get("errors") not in ([], None):
        errors.append("PRETRADE_REPORTED_ERRORS")
    for field in (
        "model_version",
        "execution_date",
        "strategy_specification_fingerprint",
    ):
        if str(audit_rotation.get(field, "")) != str(rotation.get(field, "")):
            errors.append("PRETRADE_" + field.upper() + "_MISMATCH")
    if str(audit_rotation.get("payload_sha256", "")) != _payload_sha256(rotation):
        errors.append("PRETRADE_ROTATION_PAYLOAD_SHA256_MISMATCH")
    return errors, audit


def _approved_prior_close_prices(
    rotation: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> Dict[str, float]:
    if manifest.get("approved") is not True:
        raise ValueError("market data manifest is not approved")
    data_date = str(rotation.get("data_date", ""))[:10]
    records = {
        str(item.get("code", "")): item
        for item in manifest.get("records", [])
        if isinstance(item, Mapping) and item.get("code")
    }
    prices: Dict[str, float] = {}
    for code in sorted((rotation.get("target_weights") or {})):
        item = records.get(str(code))
        if not item:
            raise ValueError(f"target {code} is missing from the market data manifest")
        source = str(item.get("source", ""))
        if not source.endswith("TENCENT_SINA_VALIDATED"):
            raise ValueError(f"target {code} has no Tencent/Sina validated price authority")
        crosscheck = ((item.get("source_validation") or {}).get("crosscheck") or {})
        if crosscheck.get("approved") is not True:
            raise ValueError(f"target {code} independent Sina crosscheck is not approved")
        if str(crosscheck.get("raw_date", ""))[:10] != data_date:
            raise ValueError(f"target {code} prior-close date does not match rotation data_date")
        try:
            price = float(crosscheck.get("raw_close"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"target {code} prior-close price is not numeric") from error
        if price <= 0.0:
            raise ValueError(f"target {code} prior-close price must be positive")
        prices[str(code)] = price
    return prices


def build_pretrade_shadow(
    rotation_path: Path,
    manifest_path: Path,
    output_path: Path,
    *,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build a prior-close execution simulation without persisting portfolio state."""
    now = generated_at or datetime.now().astimezone()
    errors = []
    rotation: Dict[str, Any] = {}
    manifest: Dict[str, Any] = {}
    prices: Dict[str, float] = {}
    orders: Dict[str, Any] = {}
    shadow_state: Dict[str, Any] = {}
    try:
        rotation = _read_object(rotation_path)
        manifest = _read_object(manifest_path)
        errors.extend(validate_rotation_contract(rotation))
        if errors:
            raise ValueError("rotation contract failed: " + "; ".join(errors[:10]))
        prices = _approved_prior_close_prices(rotation, manifest)
        reference_capital = float(rotation.get("capacity_reference_capital"))
        shadow_state = StateManager.initial()
        shadow_state.update(
            {
                "initial_capital": reference_capital,
                "cash": reference_capital,
                "peak_capital": reference_capital,
            }
        )
        orders = TradingEngine(
            shadow_state,
            rotation,
            prices,
            str(rotation.get("execution_date", ""))[:10],
            allow_rebalance=True,
            allow_valuation=True,
            source_diagnostics={
                "source": "APPROVED_PRIOR_CLOSE_PRETRADE_SHADOW",
                "quotes": {"tradeable": False, "reason": "PRIOR_CLOSE_ONLY"},
                "valuation_quotes": {"tradeable": False, "reason": "PRIOR_CLOSE_ONLY"},
                "state_write_allowed": False,
            },
        ).run()
        target_codes = set((rotation.get("target_weights") or {}).keys())
        buy_codes = {str(item.get("code")) for item in orders.get("buy_orders", [])}
        if target_codes != buy_codes:
            errors.append("shadow buy orders do not cover every target code")
        if orders.get("sell_orders"):
            errors.append("empty reference portfolio unexpectedly generated sell orders")
        if any(int(item.get("shares", 0)) % 100 for item in orders.get("buy_orders", [])):
            errors.append("shadow order violates the 100-share lot constraint")
        if any(bool(item.get("capacity_exceeded")) for item in orders.get("buy_orders", [])):
            errors.append("shadow order exceeds the execution capacity limit")
        actual_exposure = sum(float(value) for value in (orders.get("actual_weights") or {}).values())
        if actual_exposure > float(rotation.get("max_exposure_ratio", 0.0)) + 1e-6:
            errors.append("shadow portfolio exceeds the approved exposure budget")
        if float(shadow_state.get("cash", 0.0)) < -1e-6:
            errors.append("shadow portfolio has negative cash")
        if target_codes and float((orders.get("capacity_summary") or {}).get("buy_fill_ratio", 0.0)) < 0.999999:
            errors.append("shadow orders are not fully executable at the reference capital")
        blocking_reasons = [
            item
            for item in (orders.get("decision_diagnostics") or {}).get("reasons", [])
            if str(item.get("code", ""))
            not in {"LIQUIDITY_CAP_REACHED", "PORTFOLIO_ALREADY_AT_TARGET"}
        ]
        if blocking_reasons:
            errors.append("execution engine blocked the shadow plan")
    except Exception as error:
        if not errors or str(error) not in errors:
            errors.append(str(error))

    buy_orders = list(orders.get("buy_orders") or [])
    actual_weights = dict(orders.get("actual_weights") or {})
    result = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "audit_type": "non_authorising_pretrade_execution_shadow",
        "generated_at": now.isoformat(timespec="seconds"),
        "status": "READY_FOR_EXECUTION_DATE_QUOTE_REVALIDATION" if not errors else "BLOCKED",
        "shadow_only": True,
        "promotion_allowed": False,
        "order_submission_allowed": False,
        "state_persisted": False,
        "rotation": {
            "path": str(rotation_path),
            "sha256": _sha256(rotation_path) if rotation_path.is_file() else "",
            "payload_sha256": _payload_sha256(rotation) if rotation else "",
            "model_version": str(rotation.get("model_version", "")),
            "strategy_specification_fingerprint": str(
                rotation.get("strategy_specification_fingerprint", "")
            ),
            "data_date": str(rotation.get("data_date", ""))[:10],
            "execution_date": str(rotation.get("execution_date", ""))[:10],
        },
        "market_data_manifest": {
            "path": str(manifest_path),
            "sha256": _sha256(manifest_path) if manifest_path.is_file() else "",
            "payload_sha256": _payload_sha256(manifest) if manifest else "",
            "approved": manifest.get("approved") is True,
        },
        "price_basis": {
            "type": "APPROVED_PRIOR_CLOSE_FOR_SHADOW_ONLY",
            "prices": prices,
            "realtime_execution_authority": False,
        },
        "reference_capital": float(rotation.get("capacity_reference_capital", 0.0) or 0.0),
        "reference_orders": buy_orders,
        "portfolio_result": {
            "capacity_summary": dict(orders.get("capacity_summary") or {}),
            "estimated_execution_cost": float(orders.get("execution_cost_this_run", 0.0) or 0.0),
            "cash_after_estimated_execution": float(shadow_state.get("cash", 0.0) or 0.0),
            "total_assets_after_estimated_cost": orders.get("total_assets"),
            "actual_weights": actual_weights,
            "actual_total_exposure": round(sum(float(value) for value in actual_weights.values()), 6),
            "maximum_exposure": float(rotation.get("max_exposure_ratio", 0.0) or 0.0),
        },
        "errors": errors,
        "disclosures": [
            "This artifact is not an executable order file or broker instruction.",
            "Approved prior-close prices cannot grant realtime execution authority.",
            "Execution-date realtime quotes, portfolio state and all contract gates must be revalidated before trading.",
        ],
    }
    atomic_json_save(result, output_path)
    return result


__all__ = [
    "POLICY_VERSION",
    "build_pretrade_shadow",
    "validate_pretrade_shadow",
]
