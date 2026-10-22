"""Whole-share prices and explicitly verified KRX session schedules."""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import ccxt


SEOUL = ZoneInfo("Asia/Seoul")


def number(value, *, positive=False, integer=False, signed=False) -> Decimal:
    """Parse a required broker number without replacing missing values with zero."""
    try:
        if isinstance(value, bool) or value is None or value == "":
            raise ValueError
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or not math.isfinite(float(result))
            or (not signed and result < 0)
            or (positive and result <= 0)
            or (integer and result != result.to_integral_value())
        ):
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise ccxt.ExchangeError("kis invalid numeric field") from None


def tick_size(price: Decimal, asset_class: str) -> int:
    """Return the current KRX limit-price tick for stocks or ETFs."""
    if asset_class == "etf":
        return 1 if price < 2000 else 5
    if asset_class != "stock":
        raise ccxt.NotSupported("kis unsupported asset class")
    for ceiling, step in (
        (2000, 1),
        (5000, 5),
        (20000, 10),
        (50000, 50),
        (200000, 100),
        (500000, 500),
    ):
        if price < ceiling:
            return step
    return 1000


def round_price(price, asset_class: str, rounding_mode=ccxt.ROUND) -> str:
    """Round a positive limit price, checking the tick again across a price band."""
    value = number(price, positive=True)
    modes = {
        ccxt.ROUND: ROUND_HALF_UP,
        ccxt.TRUNCATE: ROUND_DOWN,
        ccxt.ROUND_UP: ROUND_UP,
        ccxt.ROUND_DOWN: ROUND_DOWN,
    }
    if rounding_mode not in modes:
        raise ccxt.BadRequest("kis unsupported price rounding mode")
    for _ in range(2):
        step = Decimal(tick_size(value, asset_class))
        rounded = (value / step).to_integral_value(rounding=modes[rounding_mode]) * step
        if rounded <= 0:
            raise ccxt.InvalidOrder("kis price rounds below the minimum tick")
        if rounded % tick_size(rounded, asset_class) == 0:
            return str(int(rounded))
        value = rounded
    raise ccxt.InvalidOrder("kis price does not satisfy the tick rule")


@dataclass(frozen=True)
class Session:
    """One independently verified opening, closing auction and closing time."""

    opening: datetime
    auction: datetime
    closing: datetime

    @classmethod
    def from_config(cls, day: date, record: dict) -> "Session":
        """Require a source and ordered minute boundaries for this exact date."""
        try:
            if not isinstance(record.get("source"), str) or not record["source"].strip():
                raise ValueError
            times = [
                datetime.strptime(day.isoformat() + " " + record[k], "%Y-%m-%d %H:%M").replace(
                    tzinfo=SEOUL
                )
                for k in ("open", "auction", "close")
            ]
            if not times[0] < times[1] < times[2]:
                raise ValueError
            return cls(*times)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ccxt.BadRequest("kis invalid verified session schedule") from None

    def minute_starts(self) -> list[int]:
        """Return continuous minute labels and the separate closing-auction label."""
        minutes = int((self.auction - self.opening).total_seconds() // 60)
        return [
            int((self.opening + timedelta(minutes=i)).timestamp() * 1000) for i in range(minutes)
        ] + [int(self.closing.timestamp() * 1000)]


def broker_timestamp(day, clock="000000") -> int:
    """Convert an explicit KIS business date and KST clock to UTC milliseconds."""
    try:
        if len(day) != 8 or len(clock) != 6:
            raise ValueError
        value = datetime.strptime(day + clock, "%Y%m%d%H%M%S").replace(tzinfo=SEOUL)
        return int(value.astimezone(UTC).timestamp() * 1000)
    except (ValueError, TypeError):
        raise ccxt.ExchangeError("kis invalid broker timestamp") from None
