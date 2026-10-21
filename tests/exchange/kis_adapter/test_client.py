import asyncio
import io
import json
import socket
import threading
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import ccxt
import pytest

from freqtrade.exchange.kis_adapter import KisAsync, KisSync
from freqtrade.exchange.kis_adapter import client as client_module
from freqtrade.exchange.kis_adapter.client import HOSTS, KisClient


CONFIG = {"apiKey": "test-key", "secret": "test-secret", "accountId": "12345678", "uid": "01"}
PATH, TR = "quotations/inquire-price", "FHKST01010100"
TOKEN = {"access_token": "test-bearer", "expires_in": 86400}
OK = {"rt_cd": "0", "output": {"stck_prpr": "100"}}


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        assert timeout == 30
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        payload, continuation = reply if isinstance(reply, tuple) else (reply, "")
        body = io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        body.headers = {"tr_cont": continuation}
        return body


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("KIS contract test attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", reject)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=1000.0, waits=[])

    def sleep(seconds):
        state.waits.append(seconds)
        state.now += seconds

    monkeypatch.setattr(
        client_module, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep)
    )
    return state


def make_client(replies, config=None):
    api = KisClient(config or CONFIG)
    transport = Transport(replies)
    api._opener = transport
    return api, transport


@pytest.mark.parametrize("environment", ["paper", "live"])
def test_environment_authentication_and_cache(environment, clock, caplog):
    config = CONFIG | {"options": {"environment": environment}}
    api, transport = make_client([TOKEN, OK, OK], config)
    for _ in range(2):
        assert api.get(PATH, TR, {"FID_INPUT_ISCD": "005930"}) == (OK, "")
    assert len(transport.calls) == 3
    assert all(r.full_url.startswith(HOSTS[environment]) for r in transport.calls)
    assert json.loads(transport.calls[0].data) == {
        "grant_type": "client_credentials",
        "appkey": "test-key",
        "appsecret": "test-secret",
    }
    headers = {k.lower(): v for k, v in transport.calls[1].header_items()}
    assert headers["authorization"] == "Bearer test-bearer"
    assert headers["tr_id"] == TR
    assert "005930" in transport.calls[1].full_url
    assert clock.waits == pytest.approx([1.1, 1.1])
    assert not any(v in caplog.text for v in CONFIG.values())


@pytest.mark.parametrize(
    "update,error",
    [
        ({"apiKey": ""}, ccxt.AuthenticationError),
        ({"secret": None}, ccxt.AuthenticationError),
        ({"accountId": "bad-account"}, ccxt.AuthenticationError),
        ({"uid": "\uff112"}, ccxt.AuthenticationError),
        ({"options": {"environment": "unknown"}}, ccxt.BadRequest),
    ],
)
def test_invalid_configuration(update, error):
    with pytest.raises(error) as exc:
        KisClient(CONFIG | update)
    assert "bad-account" not in str(exc.value)


def test_quote_only_credentials_and_shared_state_binding():
    config = {"apiKey": "key", "secret": "secret"}
    api = KisClient(config)
    api.acquire(config)
    with pytest.raises(ccxt.AuthenticationError, match="mismatch"):
        api.acquire(config | {"options": {"environment": "live"}})
    with pytest.raises(ccxt.AuthenticationError, match="mismatch"):
        api.acquire(config | {"secret": "other"})
    api.release()
    with pytest.raises(ccxt.ExchangeNotAvailable):
        api.acquire(config)


def test_expiry_and_read_authentication_retry(clock):
    api, transport = make_client([TOKEN, OK, {"rt_cd": "1", "msg_cd": "EGW00123"}, TOKEN, OK])
    api.get(PATH, TR, {})
    clock.now += 100
    assert api.get(PATH, TR, {}) == (OK, "")
    assert [r.method for r in transport.calls] == ["POST", "GET", "GET", "POST", "GET"]
    clock.now += 86400
    transport.replies.extend([TOKEN, OK])
    assert api.get(PATH, TR, {}) == (OK, "")
    assert len(transport.calls) == 7


def test_authentication_cooldown_and_single_retry(clock):
    expired = {"rt_cd": "1", "msg_cd": "EGW00123"}
    api, transport = make_client([TOKEN, expired])
    with pytest.raises(ccxt.RateLimitExceeded, match="cooldown"):
        api.get(PATH, TR, {})
    assert len(transport.calls) == 2
    clock.now += 60
    transport.replies.extend([TOKEN, expired])
    with pytest.raises(ccxt.RateLimitExceeded):
        api.get(PATH, TR, {})
    assert len(transport.calls) == 4


def test_repeated_expiry_stops_after_one_read_replay(clock):
    expired = {"rt_cd": "1", "msg_cd": "EGW00123"}
    api, transport = make_client([TOKEN, OK, expired, TOKEN, expired])
    api.get(PATH, TR, {})
    clock.now += 100
    with pytest.raises(ccxt.AuthenticationError):
        api.get(PATH, TR, {})
    assert len(transport.calls) == 5


@pytest.mark.parametrize(
    "status,error",
    [
        (302, ccxt.BadRequest),
        (400, ccxt.BadRequest),
        (401, ccxt.AuthenticationError),
        (403, ccxt.PermissionDenied),
        (429, ccxt.RateLimitExceeded),
        (503, ccxt.ExchangeNotAvailable),
    ],
)
def test_http_errors_are_sanitized(status, error, clock):
    failure = HTTPError("secret-url", status, "secret-msg", {}, io.BytesIO(b"secret-body"))
    api, transport = make_client([TOKEN, failure])
    if status == 401:
        error = ccxt.RateLimitExceeded
    with pytest.raises(error) as exc:
        api.get(PATH, TR, {"CANO": "12345678"})
    assert "secret" not in str(exc.value)
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "reply,error",
    [
        ({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "secret"}, ccxt.RateLimitExceeded),
        ({"rt_cd": "1", "msg_cd": "unknown", "msg1": "secret"}, ccxt.ExchangeError),
        ({"output": "secret"}, ccxt.ExchangeError),
        ([], ccxt.ExchangeError),
        (b"not JSON secret", ccxt.ExchangeError),
        (URLError(TimeoutError("secret")), ccxt.RequestTimeout),
        (URLError("secret"), ccxt.NetworkError),
        (ConnectionResetError("secret"), ccxt.NetworkError),
    ],
)
def test_failures_never_become_empty_success(reply, error, clock):
    api, transport = make_client([TOKEN, reply])
    with pytest.raises(error) as exc:
        api.get(PATH, TR, {})
    assert "secret" not in str(exc.value)
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "token",
    [
        {},
        {"access_token": "a", "expires_in": "nan"},
        {"access_token": "a", "expires_in": 30},
        {"access_token": "a\r\nsecret", "expires_in": 86400},
    ],
)
def test_invalid_token_response_is_not_cached(token, clock):
    api, transport = make_client([token])
    with pytest.raises(ccxt.AuthenticationError):
        api.get(PATH, TR, {})
    with pytest.raises(ccxt.RateLimitExceeded):
        api.get(PATH, TR, {})
    assert len(transport.calls) == 1


def test_mutating_endpoints_are_rejected_before_authentication(clock):
    api, transport = make_client([])
    for path in (
        "trading/order-cash",
        "trading/order-rvsecncl",
        "https://example.com",
        "../tokenP",
    ):
        with pytest.raises(ccxt.BadRequest):
            api.get(path, TR, {})
    assert not transport.calls


def test_pagination_preserves_cursor_and_fails_closed(clock):
    first = OK | {"ctx_area_fk100": "cursor-f", "ctx_area_nk100": "cursor-n"}
    api, transport = make_client([TOKEN, (first, "F"), (OK, "D")])
    params = {"CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    assert api.pages(PATH, TR, params) == [first, OK]
    assert "cursor-f" in transport.calls[-1].full_url
    assert dict(transport.calls[-1].header_items())["Tr_cont"] == "N"
    assert params == {"CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    transport.replies.extend([(first, "M"), (first, "M")])
    with pytest.raises(ccxt.ExchangeError, match="repeated"):
        api.pages(PATH, TR, params)
    transport.replies.extend([(first, "F"), {"rt_cd": "1"}])
    with pytest.raises(ccxt.ExchangeError, match="business"):
        api.pages(PATH, TR, params)
    transport.replies.append((first, "F"))
    with pytest.raises(ccxt.ExchangeError, match="limit"):
        api.pages(PATH, TR, params, max_pages=1)


@pytest.mark.asyncio
async def test_shared_facades_concurrent_authentication_and_close(clock):
    api, transport = make_client([TOKEN, OK, OK])
    sync = KisSync(CONFIG, client=api)
    asynchronous = KisAsync(CONFIG, client=api)
    assert sync.id == asynchronous.id == "kis"
    assert sync.has == asynchronous.has
    assert not any(sync.has.values())
    results = await asyncio.gather(
        asyncio.to_thread(sync._get, PATH, TR, {}), asynchronous._get(PATH, TR, {})
    )
    assert results == [(OK, ""), (OK, "")]
    assert len(transport.calls) == 3
    with pytest.raises(ccxt.NotSupported):
        sync.fetch_balance()
    with pytest.raises(ccxt.NotSupported):
        await asynchronous.create_order("005930/KRW", "limit", "buy", 1, 100)
    sync.close()
    sync.close()
    transport.replies.append(OK)
    assert await asynchronous._get(PATH, TR, {}) == (OK, "")
    await asynchronous.close()
    await asynchronous.close()
    assert api._token is None
    with pytest.raises(ccxt.ExchangeNotAvailable):
        await asynchronous._get(PATH, TR, {})


@pytest.mark.asyncio
async def test_async_cancellation_does_not_block_loop_or_leak_request_lock(clock):
    api, transport = make_client([TOKEN, OK, OK])
    asynchronous = KisAsync(CONFIG, client=api)
    entered, finish = threading.Event(), threading.Event()
    original = transport.open

    def blocking_open(request, timeout):
        entered.set()
        assert finish.wait(5)
        return original(request, timeout)

    transport.open = blocking_open
    task = asyncio.create_task(asynchronous._get(PATH, TR, {}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        finish.set()
    transport.open = original
    assert await asynchronous._get(PATH, TR, {}) == (OK, "")
    await asynchronous.close()
    assert api._closed


@pytest.mark.asyncio
async def test_cancelled_close_can_be_awaited_again(clock):
    api, _ = make_client([TOKEN, OK])
    asynchronous = KisAsync(CONFIG, client=api)
    entered, finish = threading.Event(), threading.Event()
    release = api.release

    def blocking_release():
        entered.set()
        assert finish.wait(5)
        release()

    api.release = blocking_release
    task = asyncio.create_task(asynchronous.close())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        finish.set()
    await asynchronous.close()
    assert api._closed
    assert api._owners == 0
