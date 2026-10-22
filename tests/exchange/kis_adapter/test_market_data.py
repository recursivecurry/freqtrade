import copy
import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError
from zipfile import ZIP_DEFLATED, ZipFile

import ccxt
import pytest

from freqtrade.exchange.kis_adapter import KisAsync, KisSync
from freqtrade.exchange.kis_adapter import market_data as data_module
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.market_data import KisMarketData, complete_minutes
from freqtrade.exchange.kis_adapter.market_data import _master_bytes as download_master
from freqtrade.exchange.kis_adapter.parsers import parse_candle, parse_master, parse_order_book
from freqtrade.exchange.kis_adapter.rules import (
    SEOUL,
    Session,
    broker_timestamp,
    number,
    round_price,
)


FIXTURES = Path(__file__).parents[2] / "testdata" / "kis"
NORMAL = {
    "open": "09:00",
    "auction": "15:20",
    "close": "15:30",
    "source": "synthetic test schedule",
}
CONFIG = {
    "apiKey": "test-key",
    "secret": "test-secret",
    "options": {
        "environment": "live",
        "sessions": {"2026-10-21": NORMAL, "2026-10-22": NORMAL},
    },
}


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FrozenDateTime(datetime):
    current = datetime(2026, 10, 22, 9, 1, 2, tzinfo=SEOUL)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz)


@pytest.fixture(autouse=True)
def public_sources(monkeypatch):
    monkeypatch.setattr(data_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        data_module,
        "_master_bytes",
        lambda board, day: "\n".join(load("master_" + board.lower() + "_subset.json")).encode(
            "cp949"
        ),
    )


def response(path, tr, params, continuation=""):
    if path.endswith("inquire-price"):
        return load("ticker_005930.json"), ""
    if path.endswith("inquire-asking-price-exp-ccn"):
        return load("book_005930.json")["response"], ""
    if path.endswith("inquire-daily-itemchartprice"):
        payload = load("daily_raw_005930.json")["response"]
        payload["output2"] = [
            row
            for row in payload["output2"]
            if params["FID_INPUT_DATE_1"] <= row["stck_bsop_date"] <= params["FID_INPUT_DATE_2"]
        ]
        return payload, ""
    if path.endswith("inquire-time-dailychartprice"):
        if params["FID_INPUT_DATE_1"] == "20261022":
            return load("opening_005930.json")[-1]["response"], ""
        pages = load("minutes_005930_20261021.json")
        return next(
            page["response"]
            for page in pages
            if page["params"]["FID_INPUT_HOUR_1"] == params["FID_INPUT_HOUR_1"]
        ), ""
    raise AssertionError("Unexpected broker request: " + path)


def client_and_data(config=CONFIG):
    client = KisClient(config)
    client.get = MagicMock(side_effect=response)
    return client, KisMarketData(client, config["options"])


@pytest.mark.parametrize("board", ["KOSPI", "KOSDAQ"])
def test_real_master_classifies_securities_and_preserves_codes(board):
    raw = "\n".join(load("master_" + board.lower() + "_subset.json")).encode("cp949")
    markets = parse_master(raw, board)
    assert markets
    assert all(m["info"]["securityGroup"] in ("ST", "EF") for m in markets)
    assert all(m["spot"] and not m["contract"] for m in markets)
    assert any(any(c.isalpha() for c in m["id"]) for m in markets)
    if board == "KOSPI":
        assert next(m for m in markets if m["id"] == "005930")["info"]["assetClass"] == "stock"
        assert next(m for m in markets if m["id"] == "069500")["info"]["assetClass"] == "etf"


def test_master_restrictions_and_corruption():
    row = next(r for r in load("master_kospi_subset.json") if r[:9].strip() == "005930")
    offset = len(row) - 227 + 60
    halted = row[:offset] + "Y" + row[offset + 1 :]
    assert not parse_master(halted.encode("cp949"), "KOSPI")[0]["active"]
    for raw in (b"", b"short", (row + "\n" + row).encode("cp949")):
        with pytest.raises(ccxt.ExchangeError):
            parse_master(raw, "KOSPI")


@pytest.mark.parametrize(
    "price,stock,etf",
    [
        (1999.9, "2000", "2000"),
        (2001, "2000", "2000"),
        (4999, "5000", "5000"),
        (5005, "5010", "5005"),
        (19999, "20000", "20000"),
        (49999, "50000", "50000"),
        (199999, "200000", "200000"),
        (499999, "500000", "500000"),
    ],
)
def test_price_bands_and_etf_ticks(price, stock, etf):
    assert round_price(price, "stock") == stock
    assert round_price(price, "etf") == etf


def test_price_rounding_directions_and_minimum():
    assert round_price(1999.1, "stock", ccxt.ROUND_UP) == "2000"
    assert round_price(2004, "stock", ccxt.ROUND_DOWN) == "2000"
    assert round_price(5009, "stock", ccxt.TRUNCATE) == "5000"
    with pytest.raises(ccxt.InvalidOrder):
        round_price(0.1, "stock", ccxt.TRUNCATE)


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", -1, True])
def test_invalid_required_numbers(value):
    with pytest.raises(ccxt.ExchangeError):
        number(value)
    with pytest.raises(ccxt.ExchangeError):
        number("1.2", integer=True)


@pytest.mark.asyncio
async def test_sync_async_ccxt_public_contracts():
    client, _ = client_and_data()
    sync, asynchronous = KisSync(CONFIG, client=client), KisAsync(CONFIG, client=client)
    try:
        sync.load_markets()
        asynchronous.set_markets_from_exchange(sync)
        assert "005930/KRW" in asynchronous.symbols
        assert sync.check_required_credentials()
        assert sync.market("005930/KRW")["id"] == "005930"
        assert sync.price_to_precision("005930/KRW", 223501) == "223500"
        assert asynchronous.price_to_precision("069500/KRW", 223501) == "223500"
        assert sync.amount_to_precision("005930/KRW", 1.9) == "1"
        ticker, async_ticker = (
            sync.fetch_ticker("005930/KRW"),
            await asynchronous.fetch_ticker("005930/KRW"),
        )
        for key in ("last", "bid", "ask", "percentage", "baseVolume", "quoteVolume"):
            assert ticker[key] == async_ticker[key]
        assert ticker["timestamp"] is None
        assert ticker["info"]["statisticsPeriod"] == "session"
        assert ticker["info"]["orderBookObservedAt"] >= ticker["info"]["observedAt"]
        book = sync.fetch_l2_order_book("005930/KRW", 2)
        assert book["bids"] == (await asynchronous.fetch_l2_order_book("005930/KRW", 2))["bids"]
        assert len(book["bids"]) == 2
        since = broker_timestamp("20261022", "090000")
        bars = sync.fetch_ohlcv("005930/KRW", "1m", since, 1)
        assert bars == await asynchronous.fetch_ohlcv("005930/KRW", "1m", since, 1)
        assert len(bars) == 1
        assert bars[0][0] == since
        assert bars[0][-1] == 525758
        with pytest.raises(ccxt.BadRequest):
            await asynchronous.fetch_ticker("005930/KRW", {"market": "NX"})
        with pytest.raises(ccxt.BadSymbol):
            sync.fetch_ticker("5930/KRW")
    finally:
        sync.close()
        await asynchronous.close()


def test_order_book_failures():
    payload = load("book_005930.json")["response"]
    assert parse_order_book(payload, "005930/KRW", 1)["timestamp"] is None
    for limit in (0, 11, True):
        with pytest.raises(ccxt.BadRequest):
            parse_order_book(payload, "005930/KRW", 1, limit)
    for field, value in (("askp1", "-1"), ("bidp1", "221000"), ("askp_rsqn1", "1.2")):
        broken = copy.deepcopy(payload)
        broken["output1"][field] = value
        with pytest.raises(ccxt.ExchangeError):
            parse_order_book(broken, "005930/KRW", 1)


def test_real_intraday_pages_and_price_basis_cache():
    client, data = client_and_data()
    since = broker_timestamp("20261021", "090000")
    bars = data.fetch_ohlcv("005930", "1m", since, 1000)
    assert len(bars) == 382
    assert [b[0] for b in bars] == sorted({b[0] for b in bars})
    yesterday = bars[:-1]
    assert sum(b[5] for b in yesterday) == 16086411
    assert yesterday[-1][0] == broker_timestamp("20261021", "153000")
    assert not any(
        broker_timestamp("20261021", "152000") <= b[0] < yesterday[-1][0] for b in yesterday
    )
    count = client.get.call_count
    assert data.fetch_ohlcv("005930", "1m", since, 10) == bars[:10]
    assert client.get.call_count == count
    five = data.fetch_ohlcv("005930", "5m", since, 1000)
    assert len(five) == 77
    assert five[0][0] == since
    assert five[0][5] == sum(b[5] for b in yesterday[:5])
    assert five[-1] == yesterday[-1]


def test_daily_uses_original_prices_and_excludes_current_day():
    client, data = client_and_data()
    since = broker_timestamp("20261021")
    bars = data.fetch_ohlcv("005930", "1d", since, 10)
    assert bars == [[since, 217000, 228000, 217000, 223500, 17268762]]
    assert client.get.call_args.args[2]["FID_ORG_ADJ_PRC"] == "1"
    assert (
        data.fetch_ohlcv("005930", "1d", int(FrozenDateTime.current.timestamp() * 1000) + 1, 10)
        == []
    )


def test_missing_calendar_data_and_history_failures():
    client, data = client_and_data()
    since = broker_timestamp("20261021")
    data._sessions.pop("2026-10-21")
    with pytest.raises(ccxt.ExchangeNotAvailable, match="calendar"):
        data.fetch_ohlcv("005930", "1m", since, 100)
    assert not client.get.called
    with pytest.raises(ccxt.NotSupported):
        data.fetch_ohlcv("005930", "2h", since, 100)
    with pytest.raises(ccxt.BadRequest):
        data.fetch_ohlcv("005930", "1m", since, 0)
    with pytest.raises(ccxt.NotSupported):
        data.fetch_ohlcv("005930", "1m", broker_timestamp("20200101"), 10)


def test_missing_completed_minutes_and_conflicting_duplicates():
    session = Session.from_config(date(2026, 10, 22), NORMAL)
    row = parse_candle(load("opening_005930.json")[-1]["response"]["output2"][-1], daily=False)
    now = int(FrozenDateTime.current.timestamp() * 1000)
    assert complete_minutes([row, row], session, now, "1m") == [row]
    with pytest.raises(ccxt.ExchangeError, match="missing completed"):
        complete_minutes([], session, now, "1m")
    with pytest.raises(ccxt.ExchangeError, match="conflicting"):
        complete_minutes([row, row[:-1] + [0]], session, now, "1m")
    with pytest.raises(ccxt.ExchangeError, match="outside"):
        complete_minutes([[row[0] - 60000, *row[1:]]], session, now, "1m")


def test_exceptional_schedule_and_no_partial_five_minute_bucket():
    session = Session.from_config(
        date(2026, 10, 22), NORMAL | {"open": "10:00", "auction": "16:20", "close": "16:30"}
    )
    start = int(session.opening.timestamp() * 1000)
    rows = [[start + i * 60000, 100, 110, 90, 105, 1] for i in range(5)]
    assert complete_minutes(rows, session, start + 299999, "5m") == []
    assert complete_minutes(rows, session, start + 300000, "5m") == [[start, 100, 110, 90, 105, 5]]
    with pytest.raises(ccxt.BadRequest):
        Session.from_config(date(2026, 10, 22), NORMAL | {"source": ""})


def test_corporate_actions_and_basis_disagreement_fail_closed():
    client, data = client_and_data()
    original = response

    def altered(path, tr, params, continuation=""):
        payload, header = original(path, tr, params, continuation)
        if path.endswith("inquire-daily-itemchartprice"):
            payload["output2"][0]["mod_yn"] = "Y"
        return payload, header

    client.get.side_effect = altered
    with pytest.raises(ccxt.NotSupported, match="corporate-action"):
        data.fetch_ohlcv("005930", "1m", broker_timestamp("20261021", "090000"), 500)

    def different_basis(path, tr, params, continuation=""):
        payload, header = original(path, tr, params, continuation)
        if path.endswith("inquire-daily-itemchartprice"):
            payload["output2"][0]["stck_oprc"] = "218000"
        return payload, header

    client.get.side_effect = different_basis
    with pytest.raises(ccxt.ExchangeError, match="basis"):
        data.fetch_ohlcv("005930", "1m", broker_timestamp("20261021", "090000"), 500)


def test_paper_never_uses_unverified_historical_minute_endpoint():
    config = copy.deepcopy(CONFIG)
    config["options"]["environment"] = "paper"
    client, data = client_and_data(config)
    with pytest.raises(ccxt.NotSupported, match="current day"):
        data.fetch_ohlcv("005930", "1m", broker_timestamp("20261021"), 100)
    assert not client.get.called


def test_since_limit_stops_before_unneeded_future_dates_and_cache_is_not_mutable():
    client, data = client_and_data()
    data._sessions.pop("2026-10-22")
    since = broker_timestamp("20261021", "090000")
    bars = data.fetch_ohlcv("005930", "1m", since, 1)
    assert len(bars) == 1
    assert all(
        call.args[2].get("FID_INPUT_DATE_1") != "20261022" for call in client.get.call_args_list
    )
    bars[0][4] = -1
    assert data.fetch_ohlcv("005930", "1m", since, 1)[0][4] == 219000


@pytest.mark.parametrize("failure", ["missing", "wrong_cursor", "transport"])
def test_failed_history_pages_never_return_partial_success(failure):
    client, data = client_and_data()

    def failed(path, tr, params, continuation=""):
        payload, header = response(path, tr, params, continuation)
        if path.endswith("inquire-time-dailychartprice") and params["FID_INPUT_HOUR_1"] == "132000":
            if failure == "transport":
                raise ccxt.NetworkError("synthetic outage")
            if failure == "missing":
                return {"output2": []}, ""
            return load("minutes_005930_20261021.json")[0]["response"], ""
        return payload, header

    client.get.side_effect = failed
    with pytest.raises((ccxt.ExchangeError, ccxt.NetworkError)):
        data.fetch_ohlcv("005930", "1m", broker_timestamp("20261021", "090000"), 500)
    assert not data._minute_cache


def test_empty_daily_and_closed_calendar_disagreement():
    client, data = client_and_data()
    client.get.return_value = ({"output2": []}, "")
    client.get.side_effect = None
    with pytest.raises(ccxt.ExchangeError, match="missing completed daily"):
        data.fetch_ohlcv("005930", "1d", broker_timestamp("20261021"), 10)
    client.get.side_effect = response
    data._sessions["2026-10-21"] = None
    with pytest.raises(ccxt.ExchangeError, match="contradicts"):
        data.fetch_ohlcv("005930", "1d", broker_timestamp("20261021"), 10)


def test_weekend_is_explicitly_closed_and_never_synthesized():
    client, data = client_and_data()
    data._sessions = {
        "2026-10-18": None,
        "2026-10-19": None,
        "2026-10-20": None,
        "2026-10-21": NORMAL,
        "2026-10-22": NORMAL,
    }
    since = broker_timestamp("20261018")
    bars = data.fetch_ohlcv("005930", "1m", since, 1)
    assert bars[0][0] == broker_timestamp("20261021", "090000")
    assert all(
        call.args[2].get("FID_INPUT_DATE_1") == "20261021" for call in client.get.call_args_list
    )


def test_master_download_cache_and_invalid_archives(monkeypatch):
    download_master.cache_clear()
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("kospi_code.mst", b"master bytes")
    opener = MagicMock()
    opener.open.side_effect = lambda *a, **kw: io.BytesIO(archive.getvalue())
    monkeypatch.setattr(data_module, "build_opener", lambda *handlers: opener)
    day = date(2026, 10, 22)
    try:
        assert download_master("KOSPI", day) == b"master bytes"
        assert download_master("KOSPI", day) == b"master bytes"
        assert opener.open.call_count == 1
        assert download_master("KOSPI", day + timedelta(days=1)) == b"master bytes"
        assert opener.open.call_count == 2
        download_master.cache_clear()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(b"not an archive")
        with pytest.raises(ccxt.ExchangeError, match="archive is invalid"):
            download_master("KOSPI", day)
        body = io.BytesIO(b"server text")
        opener.open.side_effect = HTTPError("url", 503, "server text", {}, body)
        with pytest.raises(ccxt.NetworkError, match="download failed"):
            download_master("KOSPI", day)
        assert body.closed
    finally:
        download_master.cache_clear()


def test_master_archive_size_limits(monkeypatch):
    download_master.cache_clear()
    opener = MagicMock()
    monkeypatch.setattr(data_module, "build_opener", lambda *handlers: opener)
    archive = io.BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
        zipped.writestr("kospi_code.mst", b"x" * 8_000_001)
    for payload in (archive.getvalue(), b"x" * 2_000_001):
        opener.open.side_effect = lambda *a, data=payload, **kw: io.BytesIO(data)
        with pytest.raises(ccxt.ExchangeError, match="size limit"):
            download_master("KOSPI", date(2026, 10, 22))
    download_master.cache_clear()


def test_daily_pagination_moves_end_date_backwards():
    client, data = client_and_data()
    data._sessions["2026-10-20"] = NORMAL

    def one_row(path, tr, params, continuation=""):
        payload, header = response(path, tr, params, continuation)
        if path.endswith("inquire-daily-itemchartprice"):
            payload["output2"] = payload["output2"][:1]
        return payload, header

    client.get.side_effect = one_row
    bars = data.fetch_ohlcv("005930", "1d", broker_timestamp("20261020"), 10)
    assert len(bars) == 2
    assert [call.args[2]["FID_INPUT_DATE_2"] for call in client.get.call_args_list] == [
        "20261022",
        "20261021",
        "20261020",
    ]
