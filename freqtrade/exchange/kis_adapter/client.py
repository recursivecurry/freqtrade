"""Serialized KIS authentication and read-only HTTP transport."""

import json
import math
import re
import threading
import time
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

import ccxt


HOSTS = {
    "paper": "https://openapivts.koreainvestment.com:29443",
    "live": "https://openapi.koreainvestment.com:9443",
}
READ_PATHS = frozenset(
    "quotations/" + name
    for name in (
        "inquire-price",
        "inquire-asking-price-exp-ccn",
        "inquire-time-dailychartprice",
        "inquire-daily-itemchartprice",
        "inquire-time-itemchartprice",
        "chk-holiday",
        "foreign-institution-total",
        "inquire-investor",
        "investor-trade-by-stock-daily",
        "inquire-member-daily",
        "program-trade-by-stock-daily",
    )
) | frozenset(
    "trading/" + name
    for name in (
        "inquire-balance",
        "inquire-psbl-order",
        "inquire-daily-ccld",
        "inquire-psbl-rvsecncl",
        "inquire-psbl-sell",
    )
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KisClient:
    """Share one token and request budget between sync and async facades.

    Credentials belong to one explicitly selected environment. Tokens stay in
    memory; separate bot processes must use separate app keys or be coordinated
    operationally. Order-changing requests require explicit paper-account mode.
    """

    def __init__(self, config: dict) -> None:
        options = config.get("options", {})
        if not isinstance(options, dict):
            raise ccxt.BadRequest("kis options must be a mapping")
        environment = options.get("environment", "paper")
        if not isinstance(environment, str) or environment not in HOSTS:
            raise ccxt.BadRequest("kis requires environment paper or live")
        api_key, secret = config.get("apiKey"), config.get("secret")
        if not all(
            isinstance(v, str) and v and all(33 <= ord(c) <= 126 for c in v)
            for v in (api_key, secret)
        ):
            raise ccxt.AuthenticationError("kis requires apiKey and secret")
        account, product = config.get("accountId", ""), config.get("uid", "")
        if (account or product) and not (
            isinstance(account, str)
            and re.fullmatch(r"\d{8}", account, flags=re.ASCII)
            and isinstance(product, str)
            and re.fullmatch(r"\d{2}", product, flags=re.ASCII)
        ):
            raise ccxt.AuthenticationError("kis requires an eight-digit account and two-digit uid")
        self.environment = environment
        self.paper_orders = options.get("enablePaperOrders", False)
        if not isinstance(self.paper_orders, bool) or (
            self.paper_orders and (environment != "paper" or not account)
        ):
            raise ccxt.PermissionDenied("kis broker writes require an explicit paper account")
        self._credentials = (api_key, secret, account, product)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._lock = threading.RLock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._last_token_attempt = -math.inf
        self._last_request = -math.inf
        self._owners = 0
        self._closed = False

    def acquire(self, config: dict) -> None:
        """Attach a facade only if its environment and credentials match."""
        with self._lock:
            if self._closed:
                raise ccxt.ExchangeNotAvailable("kis client is closed")
            supplied = tuple(config.get(k, "") for k in ("apiKey", "secret", "accountId", "uid"))
            if (
                supplied != self._credentials
                or config.get("options", {}).get("environment", "paper") != self.environment
                or config.get("options", {}).get("enablePaperOrders", False) != self.paper_orders
            ):
                raise ccxt.AuthenticationError("kis shared client configuration mismatch")
            self._owners += 1

    def release(self) -> None:
        """Release a facade after in-flight requests finish; discard the final token."""
        with self._lock:
            self._owners -= 1
            if self._owners == 0:
                if hasattr(self, "_trading"):
                    self._trading.close()
                self._closed = True
                self._token = None
                self._credentials = ("", "", "", "")

    def _http(self, method: str, path: str, *, headers=None, body=None) -> tuple[dict, str]:
        delay = 1.1 - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()
        if path == "/oauth2/tokenP":
            self._last_token_attempt = self._last_request
        request = Request(  # noqa: S310 - hosts are fixed HTTPS URLs; redirects are disabled.
            HOSTS[self.environment] + path, data=body, headers=headers or {}, method=method
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                data = json.load(response)
                continuation = response.headers.get("tr_cont", "").strip()
        except HTTPError as exc:
            status = exc.code
            exc.close()
            error = {
                401: ccxt.AuthenticationError,
                403: ccxt.PermissionDenied,
                429: ccxt.RateLimitExceeded,
            }.get(status, ccxt.ExchangeNotAvailable if status >= 500 else ccxt.BadRequest)
            raise error(f"kis HTTP {status}") from None
        except (OSError, URLError, HTTPException) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            error = ccxt.RequestTimeout if isinstance(reason, TimeoutError) else ccxt.NetworkError
            raise error("kis transport failed") from None
        except (ValueError, UnicodeError):
            raise ccxt.ExchangeError("kis returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ccxt.ExchangeError("kis returned a non-object response")
        return data, continuation

    def _authenticate(self) -> str:
        now = time.monotonic()
        if self._token and now < self._expires_at:
            return self._token
        if now < self._last_token_attempt + 60:
            raise ccxt.RateLimitExceeded("kis token issuance cooldown; retry later")
        data, _ = self._http(
            "POST",
            "/oauth2/tokenP",
            headers={"Content-Type": "application/json"},
            body=json.dumps(
                {
                    "grant_type": "client_credentials",
                    "appkey": self._credentials[0],
                    "appsecret": self._credentials[1],
                }
            ).encode(),
        )
        token, expires = data.get("access_token"), data.get("expires_in")
        try:
            lifetime = float(expires if expires is not None else 0)
        except (ValueError, TypeError):
            lifetime = 0
        if (
            not isinstance(token, str)
            or not token
            or not all(33 <= ord(c) <= 126 for c in token)
            or not math.isfinite(lifetime)
            or lifetime <= 60
        ):
            raise ccxt.AuthenticationError("kis token response is invalid")
        self._token = token
        self._expires_at = now + lifetime - 60
        return token

    def get(self, path: str, tr_id: str, params: dict, continuation: str = "") -> tuple[dict, str]:
        """Perform an authenticated read, retaining the broker continuation header.

        An expired-token response permits one read replay. Other failures are
        propagated without retries, including throttling and ambiguous timeouts.
        """
        if path not in READ_PATHS or not re.fullmatch(r"[A-Z0-9]{9,13}", tr_id):
            raise ccxt.BadRequest("kis unsupported read endpoint or TR ID")
        if continuation not in ("", "N"):
            raise ccxt.BadRequest("kis unsupported continuation request")
        with self._lock:
            if self._closed:
                raise ccxt.ExchangeNotAvailable("kis client is closed")
            for attempt in range(2):
                token = self._authenticate()
                try:
                    data, next_page = self._http(
                        "GET",
                        "/uapi/domestic-stock/v1/" + path + "?" + urlencode(params),
                        headers={
                            "authorization": "Bearer " + token,
                            "appkey": self._credentials[0],
                            "appsecret": self._credentials[1],
                            "tr_id": tr_id,
                            "custtype": "P",
                            "tr_cont": continuation,
                        },
                    )
                    if data.get("rt_cd") == "0":
                        return data, next_page
                    code = data.get("msg_cd")
                    if code == "EGW00123":
                        raise ccxt.AuthenticationError("kis token expired")
                    if code == "EGW00201":
                        raise ccxt.RateLimitExceeded("kis request limit exceeded")
                    raise ccxt.ExchangeError("kis business request failed")
                except ccxt.AuthenticationError as exc:
                    if isinstance(exc, ccxt.PermissionDenied):
                        raise
                    self._token = None
                    if attempt:
                        raise
            raise ccxt.AuthenticationError("kis authentication failed")

    def post(self, path: str, tr_id: str, body: dict) -> dict:
        """Send one paper mutation without authentication replay or transport retries."""
        allowed = {
            "trading/order-cash": ("VTTC0011U", "VTTC0012U"),
            "trading/order-rvsecncl": ("VTTC0013U",),
        }
        if (
            not self.paper_orders
            or self.environment != "paper"
            or tr_id not in allowed.get(path, ())
            or body.get("CANO") != self._credentials[2]
            or body.get("ACNT_PRDT_CD") != self._credentials[3]
            or body.get("EXCG_ID_DVSN_CD") != "KRX"
        ):
            raise ccxt.PermissionDenied("kis mutation is not authorized for this paper account")
        with self._lock:
            if self._closed:
                raise ccxt.ExchangeNotAvailable("kis client is closed")
            token = self._authenticate()
            data, _ = self._http(
                "POST",
                "/uapi/domestic-stock/v1/" + path,
                headers={
                    "authorization": "Bearer " + token,
                    "appkey": self._credentials[0],
                    "appsecret": self._credentials[1],
                    "tr_id": tr_id,
                    "custtype": "P",
                    "Content-Type": "application/json",
                },
                body=json.dumps(body).encode(),
            )
            if str(data.get("rt_cd")) != "0":
                if "rt_cd" not in data:
                    raise ccxt.ExchangeError("kis ambiguous mutation response")
                raise ccxt.InvalidOrder("kis broker rejected the paper request")
            return data

    def pages(
        self, path: str, tr_id: str, params: dict, max_pages: int = 100
    ) -> list[dict[str, Any]]:
        """Read all FK100/NK100 pages or fail without returning a partial result."""
        params = params.copy()
        pages, seen = [], set()
        continuation = ""
        for _ in range(max_pages):
            data, header = self.get(path, tr_id, params, continuation)
            pages.append(data)
            if header in ("", "D", "E"):
                return pages
            if header not in ("F", "M"):
                raise ccxt.ExchangeError("kis invalid continuation header")
            cursor = (data.get("ctx_area_fk100"), data.get("ctx_area_nk100"))
            if not all(isinstance(v, str) for v in cursor):
                raise ccxt.ExchangeError("kis missing continuation cursor")
            cursor = (str(cursor[0]).strip(), str(cursor[1]).strip())
            if not any(cursor) or cursor in seen:
                raise ccxt.ExchangeError("kis missing or repeated continuation cursor")
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
            continuation = "N"
        raise ccxt.ExchangeError("kis pagination limit reached")
