"""Synchronous CCXT facade for the local KIS adapter."""

from typing import Any

import ccxt

from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.market_data import KisMarketData
from freqtrade.exchange.kis_adapter.rules import round_price


class KisSync(ccxt.Exchange):
    """Use CCXT metadata helpers without advertising unimplemented broker methods."""

    session: Any

    def __init__(self, config: dict, *, client: KisClient | None = None) -> None:
        super().__init__({"apiKey": config.get("apiKey", ""), "secret": config.get("secret", "")})
        self.session.close()
        self.session = None
        self._client = client or KisClient(config)
        self._data = KisMarketData(self._client, config.get("options", {}))
        self._client.acquire(config)
        self._released = False
        self.precisionMode = ccxt.TICK_SIZE

    def describe(self):
        """Declare only currently implemented capabilities."""
        base = ccxt.Exchange.describe(self)
        return self.deep_extend(
            base,
            {
                "id": "kis",
                "name": "KIS (local adapter)",
                "countries": ["KR"],
                "has": {
                    name: name
                    in (
                        "publicAPI",
                        "spot",
                        "fetchMarkets",
                        "fetchTicker",
                        "fetchOrderBook",
                        "fetchL2OrderBook",
                        "fetchOHLCV",
                    )
                    for name in base["has"]
                },
                "requiredCredentials": {"apiKey": True, "secret": True},
                "timeframes": {"1m": "1m", "5m": "5m", "1d": "1d"},
            },
        )

    def _get(self, path: str, tr_id: str, params: dict) -> tuple[dict, str]:
        if self._released:
            raise ccxt.ExchangeNotAvailable("kis sync facade is closed")
        return self._client.get(path, tr_id, params)

    def fetch_markets(self, params=None):
        """Return supported equities and ETFs from KIS master metadata."""
        self._check_read(params)
        return self._data.fetch_markets()

    def _check_read(self, params):
        if self._released:
            raise ccxt.ExchangeNotAvailable("kis sync facade is closed")
        if params:
            raise ccxt.BadRequest("kis does not support additional request parameters")

    def fetch_ticker(self, symbol: str, params=None):
        """Read a ticker and its independently observed top of book."""
        self._check_read(params)
        self.load_markets()
        return self._data.fetch_ticker(self.market(symbol)["id"])

    def fetch_order_book(self, symbol: str, limit=None, params=None):
        """Read up to ten sorted KRX order-book levels."""
        self._check_read(params)
        self.load_markets()
        return self._data.fetch_order_book(self.market(symbol)["id"], limit)

    def fetch_ohlcv(self, symbol: str, timeframe="1m", since=None, limit=None, params=None):
        """Read completed candles on the explicitly verified calendar."""
        self._check_read(params)
        self.load_markets()
        return self._data.fetch_ohlcv(self.market(symbol)["id"], timeframe, since, limit)

    def price_to_precision(self, symbol: str, price):
        """Round prices using the security's current KRX price bands."""
        return round_price(price, self.market(symbol)["info"]["assetClass"])

    def close(self, clean_instance_data=False):
        """Release this facade's reference to its shared transport."""
        if not getattr(self, "_released", True):
            self._released = True
            self._client.release()
        self.apiKey = self.secret = ""
        if clean_instance_data:
            self.clean_rest_data()
