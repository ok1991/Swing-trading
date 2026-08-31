"""Small pure helpers shared by the execution modules."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .config import LOT_SIZE


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def parse_date(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def count_trading_days(from_date: str, to_date: Optional[datetime] = None) -> int:
    start = parse_date(from_date)
    if start is None:
        return 999
    end = (to_date or datetime.now()).date()
    current = start.date() + timedelta(days=1)
    count = 0
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def round_lot(shares: float) -> int:
    return max(int(shares) // LOT_SIZE * LOT_SIZE, 0)
