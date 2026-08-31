"""Remote-first V4 signal acquisition with validated cache fallback."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import MAX_SIGNAL_AGE_TRADING_DAYS, REQUEST_TIMEOUT, ROOT_DIR, SIGNALS_URL, V4_SCHEMA_VERSION
from .contract import validate_signal_contract
from .state import atomic_json_save
from .utils import count_trading_days


@dataclass
class SignalSnapshot:
    payload: Dict[str, Any]
    source: str
    source_url: str
    fetched_at: str
    allow_new_entries: bool
    stale: bool = False
    errors: List[str] = field(default_factory=list)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "update_time": self.payload.get("update_time", ""),
            "allow_new_entries": self.allow_new_entries,
            "stale": self.stale,
            "errors": list(self.errors),
        }


def _metadata_errors(payload: Dict[str, Any]) -> List[str]:
    errors = []
    for field in ("update_time", "market_policy", "market_breadth"):
        if field not in payload:
            errors.append(f"payload 缺少字段 '{field}'")
    return errors


def _is_stale(payload: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    dates = [str(signal.get("data_date", "")) for signal in payload.get("signals", [])]
    valid = [date for date in dates if date]
    if not valid:
        return True
    latest = max(valid)
    return count_trading_days(latest, now) > MAX_SIGNAL_AGE_TRADING_DAYS


def _validated(payload: Dict[str, Any]) -> List[str]:
    from jsonschema import Draft202012Validator

    schema_path = ROOT_DIR / "contracts" / "etf_signal_v4.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda error: list(error.path))
    return _metadata_errors(payload) + validate_signal_contract(payload) + [error.message for error in schema_errors]


def _blocked_payload(reason: str) -> Dict[str, Any]:
    return {
        "schema_version": V4_SCHEMA_VERSION,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_policy": {"entry_permission": "BLOCKED", "max_exposure_ratio": 0.0, "reason": reason},
        "market_breadth": {},
        "signals": [],
    }


class SignalSource:
    def __init__(self, cache_path: Path, url: str = SIGNALS_URL) -> None:
        self.cache_path = cache_path
        self.url = url

    def _snapshot(self, payload: Dict[str, Any], source: str, errors: Optional[List[str]] = None) -> SignalSnapshot:
        stale = _is_stale(payload)
        return SignalSnapshot(
            payload=payload,
            source=source,
            source_url=self.url,
            fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            allow_new_entries=not stale,
            stale=stale,
            errors=list(errors or []),
        )

    def _load_path(self, path: Path, source: str) -> SignalSnapshot:
        payload = json.loads(path.read_text(encoding="utf-8"))
        errors = _validated(payload)
        if errors:
            raise RuntimeError("V4 信号合约校验失败: " + "; ".join(errors[:20]))
        atomic_json_save(payload, self.cache_path)
        return self._snapshot(payload, source)

    def _load_cache(self, prior_errors: List[str]) -> SignalSnapshot:
        if not self.cache_path.exists():
            return SignalSnapshot(
                payload=_blocked_payload("NO_VALID_SIGNAL"),
                source="blocked",
                source_url=self.url,
                fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                allow_new_entries=False,
                stale=True,
                errors=prior_errors + ["没有可用的 V4 缓存"],
            )
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            errors = _validated(payload)
            if errors:
                raise RuntimeError("缓存合约无效: " + "; ".join(errors[:20]))
            snapshot = self._snapshot(payload, "cache", prior_errors)
            return snapshot
        except Exception as error:
            return SignalSnapshot(
                payload=_blocked_payload("INVALID_SIGNAL_CACHE"),
                source="blocked",
                source_url=self.url,
                fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                allow_new_entries=False,
                stale=True,
                errors=prior_errors + [str(error)],
            )

    def load(self, explicit_path: Optional[str] = None) -> SignalSnapshot:
        if explicit_path:
            return self._load_path(Path(explicit_path), "file")
        errors: List[str] = []
        try:
            response = requests.get(self.url, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("远程响应不是 JSON object")
            contract_errors = _validated(payload)
            if contract_errors:
                raise RuntimeError("远程 V4 合约无效: " + "; ".join(contract_errors[:20]))
            atomic_json_save(payload, self.cache_path)
            return self._snapshot(payload, "remote")
        except Exception as error:
            errors.append(str(error))
            return self._load_cache(errors)


__all__ = ["SignalSnapshot", "SignalSource"]
