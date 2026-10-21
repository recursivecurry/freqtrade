"""Synchronous CCXT facade for the local KIS adapter."""

from typing import Any

import ccxt

from freqtrade.exchange.kis_adapter.client import KisClient


class KisSync(ccxt.Exchange):
    """Use CCXT metadata helpers without advertising unimplemented broker methods."""

    session: Any

    def __init__(self, config: dict, *, client: KisClient | None = None) -> None:
        super().__init__({"apiKey": config.get("apiKey", ""), "secret": config.get("secret", "")})
        self.session.close()
        self.session = None
        self._client = client or KisClient(config)
        self._client.acquire(config)
        self._released = False

    def describe(self):
        """Declare only currently implemented capabilities."""
        base = ccxt.Exchange.describe(self)
        return self.deep_extend(
            base,
            {
                "id": "kis",
                "name": "KIS (local adapter)",
                "countries": ["KR"],
                "has": {name: False for name in base["has"]},
                "requiredCredentials": {"apiKey": True, "secret": True},
                "timeframes": {},
            },
        )

    def _get(self, path: str, tr_id: str, params: dict) -> tuple[dict, str]:
        if self._released:
            raise ccxt.ExchangeNotAvailable("kis sync facade is closed")
        return self._client.get(path, tr_id, params)

    def close(self, clean_instance_data=False):
        """Release this facade's reference to its shared transport."""
        if not getattr(self, "_released", True):
            self._released = True
            self._client.release()
        self.apiKey = self.secret = ""
        if clean_instance_data:
            self.clean_rest_data()
