"""Async CCXT facade using the same serialized transport as KisSync."""

import asyncio
from typing import Any

import ccxt
import ccxt.async_support as ccxt_async

from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.market_data import KisMarketData
from freqtrade.exchange.kis_adapter.orders import validate_market_order
from freqtrade.exchange.kis_adapter.rules import round_price
from freqtrade.exchange.kis_adapter.sync import KisSync
from freqtrade.exchange.kis_adapter.trading import KisTrading


class KisAsync(ccxt_async.Exchange):
    """Offload broker I/O so rate waits and network calls do not block the event loop."""

    session: Any

    def __init__(self, config: dict, *, client: KisClient | None = None) -> None:
        super().__init__({"apiKey": config.get("apiKey", ""), "secret": config.get("secret", "")})
        self._client = client or KisClient(config)
        self._data = KisMarketData(self._client, config.get("options", {}))
        self._client.acquire(config)
        self.session = self._client
        self.own_session = False
        self._close_task: asyncio.Task | None = None
        self.precisionMode = ccxt.TICK_SIZE
        try:
            self._trading = KisTrading.bind(self._client, config.get("options", {}))
        except BaseException:
            self.session = None
            self._client.release()
            self.apiKey = self.secret = ""
            raise
        if self._client.paper_orders:
            self.has.update(dict.fromkeys(KisTrading.CAPABILITIES, True))

    def describe(self):
        """Use the same capability contract as the synchronous facade."""
        return KisSync.describe(self)

    async def _get(self, path: str, tr_id: str, params: dict) -> tuple[dict, str]:
        if self.session is None:
            raise ccxt.ExchangeNotAvailable("kis async facade is closed")
        return await asyncio.to_thread(self._client.get, path, tr_id, params)

    def _check_read(self, params):
        if self.session is None:
            raise ccxt.ExchangeNotAvailable("kis async facade is closed")
        if params:
            raise ccxt.BadRequest("kis does not support additional request parameters")

    async def fetch_markets(self, params=None):
        """Return supported equities and ETFs without blocking the event loop."""
        self._check_read(params)
        return await asyncio.to_thread(self._data.fetch_markets)

    async def fetch_ticker(self, symbol: str, params=None):
        """Read a ticker and its independently observed top of book."""
        self._check_read(params)
        await self.load_markets()
        return await asyncio.to_thread(self._data.fetch_ticker, self.market(symbol)["id"])

    async def fetch_order_book(self, symbol: str, limit=None, params=None):
        """Read up to ten sorted KRX order-book levels."""
        self._check_read(params)
        await self.load_markets()
        return await asyncio.to_thread(
            self._data.fetch_order_book, self.market(symbol)["id"], limit
        )

    async def fetch_ohlcv(self, symbol: str, timeframe="1m", since=None, limit=None, params=None):
        """Read completed candles on the explicitly verified calendar."""
        self._check_read(params)
        await self.load_markets()
        return await asyncio.to_thread(
            self._data.fetch_ohlcv, self.market(symbol)["id"], timeframe, since, limit
        )

    def price_to_precision(self, symbol: str, price):
        """Round prices using the security's current KRX price bands."""
        return round_price(price, self.market(symbol)["info"]["assetClass"])

    async def fetch_balance(self, params=None):
        self._check_read(params)
        return await asyncio.to_thread(self._trading.fetch_balance)

    async def fetch_orders(self, symbol=None, since=None, limit=None, params=None):
        self._check_read(params)
        self._trading.validate_filters(since, limit)
        return await asyncio.to_thread(self._trading.fetch_orders, symbol, since, limit)

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self._trading.validate_filters(since, limit)
        return [
            o for o in await self.fetch_orders(symbol, since, None, params) if o["status"] == "open"
        ][:limit]

    async def fetch_order(self, id, symbol=None, params=None):  # noqa: A002
        self._check_read(params)
        return await asyncio.to_thread(self._trading.fetch_order, id, symbol)

    async def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        params = dict(params or {})
        identifier = params.pop("order", None)
        self._check_read(params)
        self._trading.validate_filters(since, limit)
        return await asyncio.to_thread(
            self._trading.fetch_my_trades, symbol, since, limit, identifier
        )

    async def create_order(self, symbol, type, side, amount, price=None, params=None):  # noqa: A002
        params = dict(params or {})
        lifetime = params.pop("timeInForce", "DAY")
        self._check_read(params)
        if lifetime != "DAY":
            raise ccxt.InvalidOrder("kis requires DAY orders")
        if not self._client.paper_orders:
            raise ccxt.NotSupported("kis paper broker orders are not enabled")
        await self.load_markets()
        validate_market_order(self.market(symbol), type, price)
        return await asyncio.to_thread(
            self._trading.create_order, symbol, type, side, amount, price
        )

    async def cancel_order(self, id, symbol=None, params=None):  # noqa: A002
        self._check_read(params)
        return await asyncio.to_thread(self._trading.cancel_order, id, symbol)

    async def close(self, clean_instance_data=False):
        """Wait for in-flight reads before releasing this facade's transport."""
        self.closed_by_user = True
        if self._close_task is None:
            self.session = None
            self.apiKey = self.secret = ""
            self._close_task = asyncio.create_task(asyncio.to_thread(self._client.release))
        await asyncio.shield(self._close_task)
        if clean_instance_data:
            self.clean_rest_data()
