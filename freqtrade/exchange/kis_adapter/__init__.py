"""Local KIS clients; not registered globally in CCXT."""

from freqtrade.exchange.kis_adapter.async_client import KisAsync
from freqtrade.exchange.kis_adapter.sync import KisSync


__all__ = ["KisSync", "KisAsync"]
