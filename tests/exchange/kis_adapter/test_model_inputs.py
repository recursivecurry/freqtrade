from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import ccxt
import pytest

from freqtrade.exchange.kis_adapter import model_inputs as module
from freqtrade.exchange.kis_adapter.client import READ_PATHS, KisClient
from freqtrade.exchange.kis_adapter.model_inputs import KisModelInputs, dated_rows, quantity
from freqtrade.exchange.kis_adapter.rules import SEOUL
from tests.exchange.kis_adapter.conftest import SCHEDULE


NOW = datetime(2026, 10, 22, 10, tzinfo=SEOUL)
SYMBOL = "005930/KRW"


def rows(day="20261022"):
    return {
        "stck_bsop_date": day,
        "ntby_qty": "0",
        "total_shnu_qty": "20",
        "total_seln_qty": "20",
        "whol_smtn_ntby_qty": "-5",
        "frgn_ntby_qty": "4",
        "orgn_ntby_qty": "3",
        "mrbn_ntby_qty": "2",
        "prsn_ntby_qty": "-8",
    }


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(module, "dt_now", lambda: NOW)
    client = KisClient({"apiKey": "test", "secret": "test", "options": {"environment": "live"}})
    client.get = MagicMock(
        return_value=({"output": [rows("20261021"), rows()], "output2": [rows("20261021")]}, "")
    )
    return KisModelInputs(client, {"sessions": {"2026-10-22": SCHEDULE}})


def test_contract_paths_dates_quantities_and_receipts(source):
    result = source.intraday(SYMBOL, ["00003", "00050"])
    assert result["members"] == {"00003": 0, "00050": 0}
    assert result["program"] == -5
    assert result["market"] == "KRX" and result["unit"] == "shares"
    assert result["source_updated_at"] is None
    assert result["freshness"] == "source_update_unknown"
    assert len(result["sources"]) == 3
    assert all(len(s["response_sha256"]) == 64 for s in result["sources"])
    for call in source.client.get.call_args_list:
        assert call.args[0] in READ_PATHS
        assert call.args[2]["FID_COND_MRKT_DIV_CODE"] == "J"


def test_history_excludes_today_and_uses_institution_total_once(source):
    result = source.history(SYMBOL, ["00003"], date(2026, 10, 1), date(2026, 10, 21))
    assert len(result["samples"]) == 1
    assert result["samples"][0]["targets"] == {"foreign": 4, "institution": 3, "individual": -8}
    with pytest.raises(ccxt.BadRequest):
        source.history(SYMBOL, ["00003"], date(2026, 10, 1), date(2026, 10, 22))


@pytest.mark.parametrize("value", [None, "", True, "NaN", "inf", "1.2", str(2**53)])
def test_invalid_quantity_is_not_zero(value):
    with pytest.raises(ccxt.ExchangeError):
        quantity(value)


@pytest.mark.parametrize(
    "bad", [{}, {"stck_bsop_date": "20261023"}, {"stck_bsop_date": "20260230"}, None]
)
def test_bad_rows_fail(bad):
    with pytest.raises(ccxt.ExchangeError):
        dated_rows({"output": [bad]}, "output", {"net": "ntby_qty"}, NOW.date())


def test_conflicting_dates_fail():
    with pytest.raises(ccxt.ExchangeError, match="conflicting"):
        dated_rows(
            {"output": [rows(), rows() | {"ntby_qty": "1"}]},
            "output",
            {"net": "ntby_qty"},
            NOW.date(),
        )


@pytest.mark.parametrize(
    "response",
    [({"output": []}, ""), ({"output": [rows("20261021")]}, ""), ({"output": [rows()]}, "M")],
)
def test_missing_today_or_incomplete_page_fails(source, response):
    source.client.get.return_value = response
    with pytest.raises(ccxt.ExchangeError):
        source.intraday(SYMBOL, ["00003"])


def test_missing_member_and_request_failure_fail_closed(source):
    source.client.get.side_effect = [
        ({"output": [rows()]}, ""),
        ({"output": []}, ""),
        ({"output": [rows()]}, ""),
    ]
    with pytest.raises(ccxt.ExchangeError, match="incomplete"):
        source.intraday(SYMBOL, ["00003", "00050"])
    source.client.get.side_effect = ccxt.RateLimitExceeded("secret")
    with pytest.raises(ccxt.RateLimitExceeded):
        source.intraday(SYMBOL, ["00003"])


def test_session_and_collection_span(source, monkeypatch):
    ticks = iter([NOW, NOW, NOW, NOW, NOW, NOW + timedelta(seconds=121)])
    monkeypatch.setattr(module, "dt_now", lambda: next(ticks))
    with pytest.raises(ccxt.ExchangeError, match="time window"):
        source.intraday(SYMBOL, ["00003"])
    monkeypatch.setattr(module, "dt_now", lambda: NOW.replace(hour=16))
    with pytest.raises(ccxt.ExchangeError, match="session"):
        source.intraday(SYMBOL, ["00003"])


def test_invalid_identity_and_paper(source):
    with pytest.raises(ccxt.BadSymbol):
        source.intraday("005930", ["00003"])
    with pytest.raises(ccxt.BadRequest):
        source.intraday(SYMBOL, ["00003", "00003"])
    with pytest.raises(ccxt.NotSupported):
        KisModelInputs(KisClient({"apiKey": "t", "secret": "t"}), {})
