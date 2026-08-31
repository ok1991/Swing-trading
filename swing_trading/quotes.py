"""Timestamped realtime ETF quote provider with fail-closed validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests

from .config import REQUEST_TIMEOUT
from .utils import safe_float


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
QUOTE_SESSION_WINDOWS = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)
MAX_QUOTE_AGE_SECONDS = 120
MAX_QUOTE_TIME_SKEW_SECONDS = 30
MAX_FUTURE_QUOTE_SKEW_SECONDS = 5


def _session_time(value: str) -> bool:
    try:
        parsed = datetime.strptime(str(value)[:8], "%H:%M:%S").time()
    except (TypeError, ValueError):
        return False
    return any(start <= parsed <= end for start, end in QUOTE_SESSION_WINDOWS)


def _shanghai_datetime(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI_TZ)
    return current.astimezone(SHANGHAI_TZ)


@dataclass
class QuoteSnapshot:
    prices: Dict[str, float]
    quote_dates: Dict[str, str]
    quote_times: Dict[str, str]
    requested_codes: List[str]
    source: str = "SINA_REALTIME"
    fetched_at: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def missing_codes(self) -> List[str]:
        return [code for code in self.requested_codes if safe_float(self.prices.get(code)) <= 0.0]

    def validation_errors(
        self,
        expected_execution_date: str,
        run_date: Optional[str] = None,
        target_data_date: Optional[str] = None,
        current_time: Optional[datetime] = None,
    ) -> List[str]:
        errors = list(self.errors)
        if self.missing_codes:
            errors.append("MISSING_QUOTES:" + ",".join(self.missing_codes))
        expected = str(expected_execution_date)[:10]
        actual_dates = {
            str(self.quote_dates.get(code, ""))[:10]
            for code in self.requested_codes
            if code in self.prices
        }
        if not expected or actual_dates != {expected}:
            errors.append(
                "QUOTE_DATE_MISMATCH:expected=" + expected + ",actual=" + ",".join(sorted(actual_dates))
            )
        invalid_times = [
            code
            for code in self.requested_codes
            if code in self.prices and not _session_time(self.quote_times.get(code, ""))
        ]
        if invalid_times:
            errors.append("QUOTE_OUTSIDE_TRADING_SESSION:" + ",".join(invalid_times))
        now = _shanghai_datetime(current_time)
        execution_date = str(run_date or now.strftime("%Y-%m-%d"))[:10]
        if execution_date != expected:
            errors.append(
                f"RUN_DATE_NOT_EXECUTION_DATE:run={execution_date},execution={expected}"
            )
        if now.strftime("%Y-%m-%d") != expected:
            errors.append(
                f"CLOCK_DATE_NOT_EXECUTION_DATE:clock={now:%Y-%m-%d},execution={expected}"
            )
        if not _session_time(now.strftime("%H:%M:%S")):
            errors.append(f"CURRENT_TIME_OUTSIDE_TRADING_SESSION:{now:%H:%M:%S}")
        quote_datetimes: Dict[str, datetime] = {}
        for code in self.requested_codes:
            if code not in self.prices:
                continue
            try:
                quote_datetimes[code] = datetime.strptime(
                    f"{str(self.quote_dates.get(code, ''))[:10]} "
                    f"{str(self.quote_times.get(code, ''))[:8]}",
                    "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=SHANGHAI_TZ)
            except (TypeError, ValueError):
                continue
        stale = [
            code
            for code, quote_time in quote_datetimes.items()
            if (now - quote_time).total_seconds() > MAX_QUOTE_AGE_SECONDS
        ]
        if stale:
            errors.append("STALE_QUOTES:" + ",".join(stale))
        future = [
            code
            for code, quote_time in quote_datetimes.items()
            if (quote_time - now).total_seconds() > MAX_FUTURE_QUOTE_SKEW_SECONDS
        ]
        if future:
            errors.append("FUTURE_QUOTES:" + ",".join(future))
        if quote_datetimes:
            timestamps = [value.timestamp() for value in quote_datetimes.values()]
            skew = max(timestamps) - min(timestamps)
            if skew > MAX_QUOTE_TIME_SKEW_SECONDS:
                errors.append(f"QUOTE_TIME_SKEW_EXCEEDED:{int(skew)}s")
        target = str(target_data_date or "")[:10]
        if target and expected <= target:
            errors.append(f"INVALID_EXECUTION_WINDOW:data={target},execution={expected}")
        return list(dict.fromkeys(errors))

    def tradeable(
        self,
        expected_execution_date: str,
        run_date: Optional[str] = None,
        target_data_date: Optional[str] = None,
        current_time: Optional[datetime] = None,
    ) -> bool:
        return not self.validation_errors(
            expected_execution_date, run_date, target_data_date, current_time
        )

    def diagnostics(
        self,
        expected_execution_date: str,
        run_date: Optional[str] = None,
        target_data_date: Optional[str] = None,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, object]:
        now = _shanghai_datetime(current_time)
        errors = self.validation_errors(
            expected_execution_date, run_date, target_data_date, now
        )
        return {
            "source": self.source,
            "fetched_at": self.fetched_at,
            "validated_at": now.strftime("%Y-%m-%d %H:%M:%S%z"),
            "max_quote_age_seconds": MAX_QUOTE_AGE_SECONDS,
            "max_quote_time_skew_seconds": MAX_QUOTE_TIME_SKEW_SECONDS,
            "requested_codes": list(self.requested_codes),
            "received_codes": sorted(self.prices),
            "missing_codes": self.missing_codes,
            "quote_dates": dict(self.quote_dates),
            "quote_times": dict(self.quote_times),
            "target_data_date": str(target_data_date or "")[:10],
            "execution_date": str(expected_execution_date)[:10],
            "tradeable": not errors,
            "errors": errors,
        }

    def valuation_diagnostics(
        self,
        valuation_date: Optional[str] = None,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, object]:
        now = _shanghai_datetime(current_time)
        expected = str(valuation_date or now.strftime("%Y-%m-%d"))[:10]
        result = self.diagnostics(
            expected,
            expected,
            None,
            now,
        )
        result["mode"] = "DAILY_MARK_TO_MARKET"
        result["valuation_date"] = expected
        return result


class RealtimeQuote:
    @staticmethod
    def _symbol(code: str) -> str:
        return ("sh" if str(code).startswith(("5", "6")) else "sz") + str(code)

    @classmethod
    def fetch(cls, codes: Iterable[str]) -> QuoteSnapshot:
        unique = [str(code) for code in dict.fromkeys(codes) if code]
        fetched_at = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S%z")
        if not unique:
            return QuoteSnapshot({}, {}, {}, [], fetched_at=fetched_at)
        url = "https://hq.sinajs.cn/list=" + ",".join(cls._symbol(code) for code in unique)
        try:
            response = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"Referer": "https://finance.sina.com.cn"},
            )
            response.raise_for_status()
            response.encoding = "gbk"
            prices: Dict[str, float] = {}
            quote_dates: Dict[str, str] = {}
            quote_times: Dict[str, str] = {}
            for line in response.text.splitlines():
                if '="' not in line:
                    continue
                symbol = line.split("hq_str_", 1)[-1].split("=", 1)[0]
                code = symbol[2:]
                fields = line.split('="', 1)[1].rstrip('";').split(",")
                price = safe_float(fields[3] if len(fields) > 3 else 0.0)
                quote_date = str(fields[30] if len(fields) > 30 else "")[:10]
                quote_time = str(fields[31] if len(fields) > 31 else "")[:8]
                if price > 0:
                    prices[code] = price
                    quote_dates[code] = quote_date
                    quote_times[code] = quote_time
            return QuoteSnapshot(
                prices,
                quote_dates,
                quote_times,
                unique,
                fetched_at=fetched_at,
            )
        except Exception as error:
            return QuoteSnapshot(
                {},
                {},
                {},
                unique,
                fetched_at=fetched_at,
                errors=[str(error)],
            )


__all__ = [
    "MAX_FUTURE_QUOTE_SKEW_SECONDS",
    "MAX_QUOTE_AGE_SECONDS",
    "MAX_QUOTE_TIME_SKEW_SECONDS",
    "QUOTE_SESSION_WINDOWS",
    "QuoteSnapshot",
    "RealtimeQuote",
]
