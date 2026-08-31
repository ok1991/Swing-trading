#!/usr/bin/env python3
"""Approved ETF rotation execution entrypoint."""

from swing_trading.cli import main_cli
from swing_trading.config import STATE_DIR, ensure_directories
from swing_trading.engine import TradingEngine
from swing_trading.locking import execution_lock
from swing_trading.rotation_contract import validate_rotation_contract
from swing_trading.state import StateManager


def run() -> None:
    ensure_directories()
    with execution_lock(STATE_DIR / "swing_execution.lock"):
        main_cli()


__all__ = ["TradingEngine", "StateManager", "run", "validate_rotation_contract"]


if __name__ == "__main__":
    run()
