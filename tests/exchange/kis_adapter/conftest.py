import copy
import socket
from datetime import datetime
from types import SimpleNamespace

import pytest

from freqtrade.enums import RunMode
from freqtrade.exchange.kis_adapter import client as client_module
from freqtrade.exchange.kis_adapter import market_data as data_module
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.rules import SEOUL
from tests.exchange.kis_adapter.test_market_data import FrozenDateTime, load, response


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


NOW = datetime(2026, 10, 22, 9, 1, 2, tzinfo=SEOUL)
SCHEDULE = {
    "open": "09:00",
    "auction": "15:20",
    "close": "15:30",
    "source": "synthetic test schedule",
    "verified_at": "2026-10-22T09:00:00+09:00",
}
COST_RULE = {
    "from": "2026-01-01",
    "through": "2026-12-31",
    "buy_commission": 0.001,
    "sell_commission": 0.001,
    "sell_tax": 0.002,
    "source": "synthetic test costs",
    "tax_treatment": "stock_transaction_tax",
}


@pytest.fixture
def kis_config(default_conf):
    config = copy.deepcopy(default_conf)
    config.update(
        dry_run=True,
        runmode=RunMode.DRY_RUN,
        stake_currency="KRW",
        timeframe="1m",
        startup_candle_count=0,
        order_time_in_force={"entry": "DAY", "exit": "DAY"},
        pairlists=[{"method": "StaticPairList"}],
    )
    config.pop("fee", None)
    config["exchange"] = {
        "name": "kis",
        "api_key": "test-key",
        "secret": "test-secret",
        "pair_whitelist": ["005930/KRW"],
        "pair_blacklist": [],
        "kis": {
            "environment": "paper",
            "sessions": {
                "2026-10-21": copy.deepcopy(SCHEDULE),
                "2026-10-22": copy.deepcopy(SCHEDULE),
            },
            "costs": {"005930/KRW": [copy.deepcopy(COST_RULE)]},
        },
    }
    config["order_types"] = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    return config


@pytest.fixture
def sources(monkeypatch, mocker):
    monkeypatch.setattr(data_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        data_module,
        "_master_bytes",
        lambda board, day: "\n".join(load("master_" + board.lower() + "_subset.json")).encode(
            "cp949"
        ),
    )
    mocker.patch("freqtrade.exchange.kis.dt_now", return_value=NOW)
    mocker.patch("freqtrade.exchange.exchange.dt_now", return_value=NOW)
    return mocker.patch.object(
        KisClient,
        "get",
        autospec=True,
        side_effect=lambda self, *args, **kwargs: response(*args, **kwargs),
    )
