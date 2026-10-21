"""Async CCXT facade using the same serialized transport as KisSync."""

import asyncio

import ccxt
import ccxt.async_support as ccxt_async

from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.sync import KisSync


class KisAsync(ccxt_async.Exchange):
    """Offload broker I/O so rate waits and network calls do not block the event loop."""

    def __init__(self, config: dict, *, client: KisClient | None = None) -> None:
        super().__init__({"apiKey": config.get("apiKey", ""), "secret": config.get("secret", "")})
        self._client = client or KisClient(config)
        self._client.acquire(config)
        self.session = self._client
        self.own_session = False
        self._close_task: asyncio.Task | None = None

    def describe(self):
        """Use the same capability contract as the synchronous facade."""
        return KisSync.describe(self)

    async def _get(self, path: str, tr_id: str, params: dict) -> tuple[dict, str]:
        if self.session is None:
            raise ccxt.ExchangeNotAvailable("kis async facade is closed")
        return await asyncio.to_thread(self._client.get, path, tr_id, params)

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
