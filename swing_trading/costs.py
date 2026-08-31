"""Execution costs aligned with the accepted ETF rotation backtest."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from .config import (
    BASE_SLIPPAGE_BPS,
    BID_ASK_HALF_SPREAD_BPS,
    COMMISSION_RATE,
    EXCHANGE_HANDLING_RATE,
    IMPACT_BPS_AT_FULL_ADV,
    LOT_SIZE,
    MAX_PARTICIPATION_RATE,
    MINIMUM_COMMISSION,
)


@dataclass(frozen=True)
class ExecutionCostModel:
    """China ETF execution model: 1.5 bps commission with no minimum fee."""

    commission_rate: float = COMMISSION_RATE
    minimum_commission: float = MINIMUM_COMMISSION
    exchange_handling_rate: float = EXCHANGE_HANDLING_RATE
    bid_ask_half_spread_bps: float = BID_ASK_HALF_SPREAD_BPS
    base_slippage_bps: float = BASE_SLIPPAGE_BPS
    impact_bps_at_full_adv: float = IMPACT_BPS_AT_FULL_ADV
    max_participation_rate: float = MAX_PARTICIPATION_RATE
    lot_size: int = LOT_SIZE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def round_lot(self, shares: float) -> int:
        lot = max(1, int(self.lot_size))
        return max(0, int(float(shares) // lot) * lot)

    def capacity_lot(self, price: float, average_daily_amount: Optional[float]) -> int:
        price_value = max(float(price), 0.0)
        adv = max(float(average_daily_amount or 0.0), 0.0)
        if price_value <= 0.0 or adv <= 0.0:
            return 0
        return self.round_lot(adv * max(float(self.max_participation_rate), 0.0) / price_value)

    def estimate(
        self,
        side: str,
        price: float,
        shares: int,
        average_daily_amount: Optional[float] = None,
    ) -> Dict[str, float]:
        side = str(side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        gross = max(0.0, float(price)) * max(0, int(shares))
        if gross <= 0:
            return {
                "gross": 0.0,
                "commission": 0.0,
                "other_fees": 0.0,
                "slippage": 0.0,
                "total_cost": 0.0,
                "cash_delta": 0.0,
                "effective_price": 0.0,
                "average_daily_amount": max(float(average_daily_amount or 0.0), 0.0),
                "participation_rate": 0.0,
                "requested_participation_rate": 0.0,
                "capacity_exceeded": False,
                "impact_bps": 0.0,
            }
        commission = max(float(self.minimum_commission), gross * float(self.commission_rate))
        other_fees = gross * float(self.exchange_handling_rate)
        adv = max(float(average_daily_amount or 0.0), gross)
        requested_participation = gross / adv
        participation = min(requested_participation, max(0.0, float(self.max_participation_rate)))
        impact_bps = float(self.impact_bps_at_full_adv) * math.sqrt(max(participation, 0.0))
        slippage_rate = (
            float(self.bid_ask_half_spread_bps)
            + float(self.base_slippage_bps)
            + impact_bps
        ) / 10000.0
        slippage = gross * slippage_rate
        total_cost = commission + other_fees + slippage
        cash_delta = -(gross + total_cost) if side == "BUY" else gross - total_cost
        effective_price = (
            (gross + total_cost) / shares if side == "BUY" else (gross - total_cost) / shares
        )
        return {
            "gross": gross,
            "commission": commission,
            "other_fees": other_fees,
            "slippage": slippage,
            "total_cost": total_cost,
            "cash_delta": cash_delta,
            "effective_price": effective_price,
            "average_daily_amount": max(float(average_daily_amount or 0.0), 0.0),
            "participation_rate": participation,
            "requested_participation_rate": requested_participation,
            "capacity_exceeded": requested_participation > float(self.max_participation_rate) + 1e-12,
            "impact_bps": impact_bps,
        }


DEFAULT_COST_MODEL = ExecutionCostModel()


__all__ = ["DEFAULT_COST_MODEL", "ExecutionCostModel"]
