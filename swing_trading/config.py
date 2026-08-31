"""Environment driven configuration for QingLong and local runs."""

from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE settings without an extra dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(ROOT_DIR / ".env")

RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", ROOT_DIR / "runtime"))
STATE_DIR = Path(os.environ.get("STATE_DIR", RUNTIME_DIR / "state"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", RUNTIME_DIR / "cache"))
LOG_DIR = Path(os.environ.get("LOG_DIR", RUNTIME_DIR / "logs"))
PUBLIC_DIR = Path(os.environ.get("PUBLIC_DIR", ROOT_DIR / "public"))

STATE_FILE = os.environ.get("STATE_FILE", "portfolio_state.json")
ROTATION_FILE = os.environ.get("ROTATION_FILE", "etf_rotation_latest.json")
TRADE_REPORT_FILE = os.environ.get("TRADE_REPORT_FILE", "index.html")
EXECUTION_FEEDBACK_FILE = os.environ.get(
    "EXECUTION_FEEDBACK_FILE", "execution_feedback_latest.json"
)
EXECUTION_FEEDBACK_HISTORY_FILE = os.environ.get(
    "EXECUTION_FEEDBACK_HISTORY_FILE", "execution_feedback_history.json"
)
EXECUTION_PLAN_FILE = os.environ.get("EXECUTION_PLAN_FILE", "execution_plan_latest.json")
LIVE_PERFORMANCE_FILE = os.environ.get(
    "LIVE_PERFORMANCE_FILE", "live_performance_latest.json"
)
ROTATION_URL = os.environ.get("ROTATION_URL", "https://etf.imlam.com/etf_rotation_latest.json")
ROTATION_SOURCE_PRIORITY = os.environ.get(
    "ROTATION_SOURCE_PRIORITY", "local_first"
).strip().lower()
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "15"))

INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "10000"))
COMMISSION_RATE = float(os.environ.get("COMMISSION_RATE", "0.00015"))
MINIMUM_COMMISSION = float(os.environ.get("MINIMUM_COMMISSION", "0"))
EXCHANGE_HANDLING_RATE = float(os.environ.get("EXCHANGE_HANDLING_RATE", "0.00004"))
BID_ASK_HALF_SPREAD_BPS = float(os.environ.get("BID_ASK_HALF_SPREAD_BPS", "2"))
BASE_SLIPPAGE_BPS = float(os.environ.get("BASE_SLIPPAGE_BPS", "3"))
IMPACT_BPS_AT_FULL_ADV = float(os.environ.get("IMPACT_BPS_AT_FULL_ADV", "18"))
MAX_PARTICIPATION_RATE = float(os.environ.get("MAX_PARTICIPATION_RATE", "0.10"))
LOT_SIZE = int(os.environ.get("LOT_SIZE", "100"))

MAX_ROTATION_AGE_TRADING_DAYS = int(os.environ.get("MAX_ROTATION_AGE_TRADING_DAYS", "2"))
MAX_ROTATION_GENERATED_AGE_HOURS = int(
    os.environ.get("MAX_ROTATION_GENERATED_AGE_HOURS", "96")
)
ROTATION_SCHEMA_VERSION = 2
ROTATION_EXECUTION_POLICY_VERSION = "single-exposure-authority-v4"
ROTATION_ACCEPTANCE_POLICY_VERSION = "rolling-excess-stability-v1"
STATE_SCHEMA_VERSION = 8


def ensure_directories() -> None:
    for path in (STATE_DIR, CACHE_DIR, LOG_DIR, PUBLIC_DIR):
        path.mkdir(parents=True, exist_ok=True)


def state_path() -> Path:
    return STATE_DIR / STATE_FILE


def cache_path() -> Path:
    return CACHE_DIR / ROTATION_FILE


def local_rotation_source_path() -> Path | None:
    """Prefer the sibling ETF production artifact for same-host deployments."""
    configured = os.environ.get("LOCAL_ROTATION_SOURCE")
    if configured is not None:
        value = configured.strip()
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else ROOT_DIR / path
    sibling = ROOT_DIR.parent / "ETF-main" / "public" / ROTATION_FILE
    return sibling if sibling.is_file() else None


def pretrade_audit_path(execution_date: str) -> Path:
    configured = os.environ.get("PRETRADE_AUDIT")
    if configured is not None and configured.strip():
        path = Path(configured.strip())
        return path if path.is_absolute() else ROOT_DIR / path
    compact_date = str(execution_date)[:10].replace("-", "")
    return (
        ROOT_DIR.parent
        / "ETF-main"
        / ".runtime"
        / "audits"
        / f"pretrade_shadow_{compact_date}.json"
    )


def report_path() -> Path:
    return PUBLIC_DIR / TRADE_REPORT_FILE


def execution_feedback_path() -> Path:
    return PUBLIC_DIR / EXECUTION_FEEDBACK_FILE


def execution_feedback_history_path() -> Path:
    return PUBLIC_DIR / EXECUTION_FEEDBACK_HISTORY_FILE


def execution_feedback_virtual_path() -> Path:
    return PUBLIC_DIR / "virtual_broker_fills_latest.json"


def execution_plan_path() -> Path:
    return STATE_DIR / EXECUTION_PLAN_FILE


def execution_plan_archive_dir() -> Path:
    return STATE_DIR / "execution_plans"


def live_performance_path() -> Path:
    return PUBLIC_DIR / LIVE_PERFORMANCE_FILE
