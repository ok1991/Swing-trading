"""Remote-first acquisition of the approved ETF rotation target."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import (
    MAX_ROTATION_AGE_TRADING_DAYS,
    MAX_ROTATION_GENERATED_AGE_HOURS,
    REQUEST_TIMEOUT,
    ROOT_DIR,
    ROTATION_SCHEMA_VERSION,
    ROTATION_URL,
)
from .rotation_contract import validate_rotation_contract
from .state import atomic_json_save
from .utils import count_trading_days


@dataclass
class RotationSnapshot:
    payload: Dict[str, Any]
    source: str
    source_url: str
    fetched_at: str
    allow_rebalance: bool
    stale: bool = False
    errors: List[str] = field(default_factory=list)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "data_date": self.payload.get("data_date", ""),
            "execution_date": self.payload.get("execution_date", ""),
            "model_version": self.payload.get("model_version", ""),
            "execution_policy_version": self.payload.get("execution_policy_version", ""),
            "acceptance_policy_version": self.payload.get("acceptance_policy_version", ""),
            "strategy_specification_fingerprint": self.payload.get(
                "strategy_specification_fingerprint", ""
            ),
            "allow_rebalance": self.allow_rebalance,
            "stale": self.stale,
            "errors": list(self.errors),
        }


def _is_stale(payload: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    current = now or datetime.now()
    try:
        generated_at = datetime.strptime(
            str(payload.get("generated_at", ""))[:19],
            "%Y-%m-%d %H:%M:%S",
        )
        generation_age_seconds = (current - generated_at).total_seconds()
        generated_stale = (
            generation_age_seconds < -600
            or generation_age_seconds > MAX_ROTATION_GENERATED_AGE_HOURS * 3600
        )
    except (TypeError, ValueError):
        generated_stale = True
    return generated_stale or (
        count_trading_days(str(payload.get("data_date", "")), now)
        > MAX_ROTATION_AGE_TRADING_DAYS
    )


def _validated(payload: Dict[str, Any]) -> List[str]:
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (ROOT_DIR / "contracts" / "etf_rotation_v2.schema.json").read_text(encoding="utf-8")
    )
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    return validate_rotation_contract(payload) + [error.message for error in schema_errors]


def _payload_sha256(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def sync_local_rotation_cache(
    source_path: Path,
    cache_path: Path,
    *,
    audit_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Atomically sync only a current, fully valid local V2 authority into Swing."""
    before_bytes = cache_path.read_bytes() if cache_path.is_file() else None
    result: Dict[str, Any] = {
        "schema_version": 1,
        "policy_version": "strict-local-rotation-cache-sync-v1",
        "generated_at": (now or datetime.now().astimezone()).astimezone().isoformat(
            timespec="seconds"
        ),
        "source_path": str(source_path.resolve()),
        "cache_path": str(cache_path.resolve()),
        "status": "BLOCKED",
        "cache_updated": False,
        "cache_preserved_on_failure": True,
        "errors": [],
    }
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("local rotation source is not a JSON object")
        errors = _validated(payload)
        if errors:
            raise ValueError("local rotation contract invalid: " + "; ".join(errors[:20]))
        if _is_stale(payload, now):
            raise ValueError("local rotation source is stale or future-dated")
        payload_hash = _payload_sha256(payload)
        result.update(
            {
                "model_version": str(payload.get("model_version", "")),
                "execution_date": str(payload.get("execution_date", ""))[:10],
                "strategy_specification_fingerprint": str(
                    payload.get("strategy_specification_fingerprint", "")
                ),
                "payload_sha256": payload_hash,
            }
        )
        current_hash = ""
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    current_hash = _payload_sha256(cached)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                current_hash = ""
        if current_hash == payload_hash:
            result["status"] = "ALREADY_CURRENT"
        else:
            atomic_json_save(payload, cache_path)
            verified = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(verified, dict) or _payload_sha256(verified) != payload_hash:
                raise RuntimeError("rotation cache readback verification failed")
            result["status"] = "SYNCED"
            result["cache_updated"] = True
    except Exception as error:
        result["errors"] = [str(error)[:2000]]
        after_bytes = cache_path.read_bytes() if cache_path.is_file() else None
        result["cache_preserved_on_failure"] = before_bytes == after_bytes
    if audit_path is not None:
        atomic_json_save(result, audit_path)
    return result


def _blocked_payload(reason: str) -> Dict[str, Any]:
    return {
        "schema_version": ROTATION_SCHEMA_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "approved": False,
        "reason": reason,
        "target_weights": {},
    }


class RotationSource:
    def __init__(
        self,
        cache_path: Path,
        url: str = ROTATION_URL,
        local_path: Optional[Path] = None,
        prefer_local: bool = True,
    ) -> None:
        self.cache_path = cache_path
        self.url = url
        self.local_path = Path(local_path) if local_path is not None else None
        self.prefer_local = bool(prefer_local)

    def _snapshot(
        self,
        payload: Dict[str, Any],
        source: str,
        errors: Optional[List[str]] = None,
        source_url: Optional[str] = None,
    ) -> RotationSnapshot:
        stale = _is_stale(payload)
        return RotationSnapshot(
            payload=payload,
            source=source,
            source_url=source_url or self.url,
            fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            allow_rebalance=not stale,
            stale=stale,
            errors=list(errors or []),
        )

    def _load_path(self, path: Path, source: str) -> RotationSnapshot:
        payload = json.loads(path.read_text(encoding="utf-8"))
        errors = _validated(payload)
        if errors:
            raise RuntimeError("轮动目标合约校验失败: " + "; ".join(errors[:20]))
        atomic_json_save(payload, self.cache_path)
        return self._snapshot(
            payload,
            source,
            source_url=str(path.resolve()),
        )

    def _load_local(self, errors: List[str]) -> Optional[RotationSnapshot]:
        if self.local_path is None or not self.local_path.is_file():
            return None
        try:
            snapshot = self._load_path(self.local_path, "local_sibling")
            if snapshot.allow_rebalance:
                return snapshot
            errors.append("LOCAL_ROTATION_STALE:" + str(self.local_path.resolve()))
        except Exception as error:
            errors.append("LOCAL_ROTATION_INVALID:" + str(error))
        return None

    def _load_cache(self, prior_errors: List[str]) -> RotationSnapshot:
        if not self.cache_path.exists():
            return RotationSnapshot(
                payload=_blocked_payload("NO_VALID_ROTATION_TARGET"),
                source="blocked",
                source_url=self.url,
                fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                allow_rebalance=False,
                stale=True,
                errors=prior_errors + ["没有可用的轮动目标缓存"],
            )
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            errors = _validated(payload)
            if errors:
                raise RuntimeError("轮动缓存无效: " + "; ".join(errors[:20]))
            return self._snapshot(payload, "cache", prior_errors)
        except Exception as error:
            return RotationSnapshot(
                payload=_blocked_payload("INVALID_ROTATION_CACHE"),
                source="blocked",
                source_url=self.url,
                fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                allow_rebalance=False,
                stale=True,
                errors=prior_errors + [str(error)],
            )

    def load(self, explicit_path: Optional[str] = None) -> RotationSnapshot:
        if explicit_path:
            return self._load_path(Path(explicit_path), "file")
        errors: List[str] = []
        if self.prefer_local:
            local = self._load_local(errors)
            if local is not None:
                return local
        try:
            response = requests.get(
                self.url,
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("远程响应不是 JSON object")
            contract_errors = _validated(payload)
            if contract_errors:
                raise RuntimeError("远程轮动目标无效: " + "; ".join(contract_errors[:20]))
            atomic_json_save(payload, self.cache_path)
            return self._snapshot(payload, "remote", errors)
        except Exception as error:
            errors.append(str(error))
            if not self.prefer_local:
                local = self._load_local(errors)
                if local is not None:
                    return local
            return self._load_cache(errors)


__all__ = ["RotationSnapshot", "RotationSource", "sync_local_rotation_cache"]
