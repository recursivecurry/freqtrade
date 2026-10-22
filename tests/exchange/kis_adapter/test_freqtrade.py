from datetime import UTC, datetime, timedelta

import ccxt
import pandas as pd
import pytest

from freqtrade.configuration import TimeRange
from freqtrade.data.dataprovider import DataProvider
from freqtrade.data.history import load_pair_history
from freqtrade.data.history.datahandlers import get_datahandler
from freqtrade.enums import CandleType, RunMode
from freqtrade.exceptions import ConfigurationError, InvalidOrderException, OperationalException
from freqtrade.exchange import Exchange, Kis
from freqtrade.exchange.check_exchange import check_exchange
from freqtrade.exchange.exchange_utils import (
    is_exchange_known,
    is_exchange_known_ccxt,
    list_available_exchanges,
    uses_continuous_candles,
)
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.costs import estimate_cost
from freqtrade.exchange.kis_adapter.rules import SEOUL, broker_timestamp
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.persistence import LocalTrade
from freqtrade.resolvers import ExchangeResolver, StrategyResolver
from tests.exchange.kis_adapter.conftest import COST_RULE, NOW, SCHEDULE


def test_local_registration_and_loader_preserve_credentials(kis_config, sources):
    assert is_exchange_known("kis")
    assert not is_exchange_known_ccxt("kis")
    assert not uses_continuous_candles("kis")
    assert uses_continuous_candles("kraken")
    assert check_exchange(kis_config)
    entry = next(e for e in list_available_exchanges(True) if e["classname"] == "kis")
    assert entry["valid"] and not entry["supported"]
    assert "dry-run/paper" in entry["comment"] and "production writes disabled" in entry["comment"]
    exchange = ExchangeResolver.load_exchange(kis_config)
    try:
        assert isinstance(exchange, Kis)
        assert exchange._api._client is exchange._api_async._client
        assert exchange._api.apiKey == "test-key"
        assert exchange._api._client.environment == "paper"
        assert not exchange.exchange_has("createOrder")
        assert exchange.price_to_precision("005930/KRW", 223751) == 224000
        assert exchange.price_get_one_pip("005930/KRW", 223751) == 500
    finally:
        exchange.close()
    assert exchange._api._client._closed


@pytest.mark.parametrize(
    "change",
    [
        {"dry_run": False, "runmode": RunMode.LIVE},
        {"order_time_in_force": {"entry": "GTC", "exit": "DAY"}},
        {"fee": 0.001},
        {"pairlists": [{"method": "VolumePairList"}]},
    ],
)
def test_unsupported_configuration_fails(kis_config, sources, change, mocker):
    release = mocker.spy(KisClient, "release")
    kis_config.update(change)
    with pytest.raises((ConfigurationError, OperationalException)):
        Kis(kis_config)
    if change.get("dry_run") is not False:
        assert release.call_count == 2
        assert release.call_args.args[0]._closed


def test_virtual_orders_have_day_lifetime_asymmetric_costs_and_no_broker_writes(
    kis_config, sources, mocker
):
    exchange = Kis(kis_config)
    try:
        buy = exchange.create_order(
            pair="005930/KRW",
            ordertype="market",
            side="buy",
            amount=1.9,
            rate=223500,
            leverage=1,
            time_in_force="DAY",
        )
        assert buy["status"] == "closed" and buy["filled"] == 1
        assert buy["timeInForce"] == "DAY"
        assert buy["fee"]["cost"] == 224
        assert buy["info"]["estimatedCosts"]["estimated"]
        sell = exchange.create_order(
            pair="005930/KRW",
            ordertype="market",
            side="sell",
            amount=1,
            rate=223000,
            leverage=1,
            time_in_force="DAY",
        )
        assert sell["fee"]["cost"] == 669
        pending = exchange.create_order(
            pair="005930/KRW",
            ordertype="limit",
            side="buy",
            amount=1,
            rate=200000,
            leverage=1,
            time_in_force="DAY",
        )
        assert pending["status"] == "open"
        calls = sources.call_count
        mocker.patch("freqtrade.exchange.kis.dt_now", return_value=NOW.replace(hour=16))
        expired = exchange.fetch_dry_run_order(pending["id"])
        assert expired["status"] == "expired" and expired["filled"] == 0
        assert expired["remaining"] == 0
        assert sources.call_count == calls
        with pytest.raises(InvalidOrderException):
            exchange.create_order(
                pair="005930/KRW",
                ordertype="market",
                side="buy",
                amount=1,
                rate=223500,
                leverage=1,
                time_in_force="DAY",
            )
        assert all(
            "order-cash" not in c.args[1] and "order-rvsecncl" not in c.args[1]
            for c in sources.call_args_list
        )
    finally:
        exchange.close()


def test_calendar_freshness_exceptional_hours_and_day_rollover(kis_config, sources):
    exchange = Kis(kis_config)
    try:
        assert exchange.can_place_order("005930/KRW", NOW)
        assert not exchange.can_place_order("005930/KRW", NOW.replace(hour=8))
        assert not exchange.can_place_order("005930/KRW", NOW.replace(hour=16))
        assert not exchange.can_place_order("005930/KRW", NOW + timedelta(days=1))
        record = exchange._kis_options["sessions"]["2026-10-22"]
        record["verified_at"] = "2026-10-20T09:00:00+09:00"
        assert not exchange.can_place_order("005930/KRW", NOW)
        assert exchange.order_expired("005930/KRW", NOW, NOW + timedelta(days=1))
        assert not exchange.order_expired(
            "005930/KRW", NOW, NOW.replace(hour=15, minute=30, second=0)
        )
        assert exchange.order_expired("005930/KRW", NOW, NOW.replace(hour=15, minute=30, second=1))
    finally:
        exchange.close()


def test_live_cache_merge_does_not_fill_closed_sessions_or_floor_daily_labels(kis_config, sources):
    exchange = Kis(kis_config)
    try:
        a, b = broker_timestamp("20261021", "153000"), broker_timestamp("20261022", "090000")
        first = [[a, 100, 100, 100, 100, 1]]
        second = [[b, 101, 101, 101, 101, 2]]
        exchange._process_ohlcv_df(
            "005930/KRW", "1m", CandleType.SPOT, first, True, False, b + 60000
        )
        merged = exchange._process_ohlcv_df(
            "005930/KRW", "1m", CandleType.SPOT, second, True, False, b + 60000
        )
        assert len(merged) == 2
        assert list(merged["volume"]) == [1, 2]
        stamp = broker_timestamp("20261021")
        daily = exchange._process_ohlcv_df(
            "005930/KRW", "1d", CandleType.SPOT, [[stamp, 100, 100, 100, 100, 1]], False, False, b
        )
        assert int(daily.iloc[0]["date"].timestamp() * 1000) == stamp
    finally:
        exchange.close()


def test_cost_rules_are_dated_sided_and_fail_closed():
    profiles = {"005930/KRW": [COST_RULE]}
    assert estimate_cost(profiles, "005930/KRW", "buy", 2, 10000, NOW)["cost"] == 20
    assert estimate_cost(profiles, "005930/KRW", "sell", 2, 10000, NOW)["cost"] == 60
    next_rule = COST_RULE | {"from": "2027-01-01", "through": "2027-12-31", "sell_tax": 0.003}
    profiles["005930/KRW"].append(next_rule)
    assert (
        estimate_cost(profiles, "005930/KRW", "sell", 2, 10000, NOW.replace(year=2027))["cost"]
        == 80
    )
    with pytest.raises(ccxt.BadRequest):
        estimate_cost(profiles, "005930/KRW", "sell", 2, 10000, NOW.replace(year=2028))
    with pytest.raises(ccxt.NotSupported):
        estimate_cost(
            {"005930/KRW": [COST_RULE | {"tax_treatment": "nav_withholding"}]},
            "005930/KRW",
            "sell",
            2,
            10000,
            NOW,
        )


def test_backtest_uses_whole_shares_band_ticks_and_costs_at_each_trade_date(
    kis_config, sources, mocker
):
    kis_config.update(
        runmode=RunMode.BACKTEST,
        stake_amount=500000,
        dry_run_wallet=1000000,
        max_open_trades=1,
    )
    first = COST_RULE | {"through": "2026-10-21"}
    second = COST_RULE | {"from": "2026-10-22", "sell_tax": 0.003}
    kis_config["exchange"]["kis"]["costs"] = {"005930/KRW": [first, second]}
    exchange = Kis(kis_config)
    try:
        backtest = Backtesting(kis_config, exchange=exchange)
        backtest._set_strategy(backtest.strategylist[0])
        row = [
            pd.Timestamp("2026-10-21T09:00:00+09:00"),
            223751,
            225000,
            222000,
            224000,
            1,
            0,
            0,
            0,
            "",
            "",
        ]
        trade = backtest._enter_trade("005930/KRW", row, "long")
        assert trade.amount == 2 and trade.open_rate == 224000
        assert trade.fee_open == 0.001 and trade.fee_close == 0.003
        assert (
            backtest._enter_trade(
                "005930/KRW", [pd.Timestamp("2026-10-21T16:00:00+09:00"), *row[1:]], "long"
            )
            is None
        )
        next_row = [pd.Timestamp("2026-10-22T09:00:00+09:00"), *row[1:]]
        backtest._exit_trade(trade, next_row, 223751, 2, "dated_cost_test")
        assert trade.fee_close == 0.004
        assert trade.orders[-1].price == 224000
        assert trade.orders[-1].cost == 449792
    finally:
        Backtesting.cleanup()
        exchange.close()


def test_dataset_validation_and_daily_publication_use_verified_sessions(kis_config, sources):
    exchange = Kis(kis_config)
    try:
        complete = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-10-22T00:00:00Z", "2026-10-22T00:01:00Z", "2026-10-22T00:02:00Z"]
                )
            }
        )
        exchange.validate_candle_data("005930/KRW", "1m", complete)
        with pytest.raises(OperationalException, match="session gap"):
            exchange.validate_candle_data("005930/KRW", "1m", complete.iloc[[0, 2]])
        assert exchange._daily_publication_cutoff(NOW.replace(hour=8)) is None
        assert exchange._daily_publication_cutoff(NOW) == broker_timestamp("20261021")
        assert exchange.candle_execution_time(
            "1d", datetime(2026, 10, 22, tzinfo=SEOUL)
        ) == datetime(2026, 10, 22, tzinfo=UTC)
    finally:
        exchange.close()


def test_daily_signal_freshness_spans_weekends_but_rejects_missing_sessions(
    kis_config, sources, mocker
):
    kis_config["exchange"]["kis"]["sessions"].update(
        {
            "2026-10-15": SCHEDULE,
            "2026-10-16": SCHEDULE,
            "2026-10-17": None,
            "2026-10-18": None,
            "2026-10-19": SCHEDULE,
        }
    )
    exchange = Kis(kis_config)
    try:
        strategy = StrategyResolver.load_strategy(kis_config)
        strategy.dp = DataProvider(kis_config, exchange)
        monday = datetime(2026, 10, 19, 9, 1, tzinfo=SEOUL)
        mocker.patch("freqtrade.strategy.interface.dt_now", return_value=monday)
        frame = pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-10-16T00:00:00+09:00")],
                "enter_long": [1],
                "exit_long": [0],
            }
        )
        assert strategy.get_latest_candle("005930/KRW", "1d", frame)[0] is not None
        assert strategy.get_entry_signal("005930/KRW", "1d", frame)[0] is not None
        strategy.ignore_buying_expired_candle_after = 90
        assert strategy.get_entry_signal("005930/KRW", "1d", frame)[0] is not None
        mocker.patch("freqtrade.strategy.interface.dt_now", return_value=monday.replace(minute=2))
        assert strategy.get_entry_signal("005930/KRW", "1d", frame)[0] is None
        mocker.patch("freqtrade.strategy.interface.dt_now", return_value=monday)
        old = frame.copy()
        old["date"] = pd.to_datetime(["2026-10-15T00:00:00+09:00"])
        assert strategy.get_latest_candle("005930/KRW", "1d", old) == (None, None)
        exchange._api._data._sessions.pop("2026-10-18")
        assert strategy.get_latest_candle("005930/KRW", "1d", frame) == (None, None)
    finally:
        exchange.close()


@pytest.mark.parametrize("forced, detail_minutes", [(False, 0), (True, 0), (False, 5)])
def test_daily_backtest_fills_close_and_callbacks_share_the_session_clock(
    kis_config, sources, mocker, forced, detail_minutes
):
    kis_config.update(
        runmode=RunMode.BACKTEST,
        timeframe="1d",
        stake_amount=500000,
        dry_run_wallet=1000000,
        max_open_trades=1,
    )
    exchange = Kis(kis_config)
    try:
        backtest = Backtesting(kis_config, exchange=exchange)
        backtest._set_strategy(backtest.strategylist[0])
        filled = mocker.patch.object(backtest.strategy, "order_filled")
        row = [
            pd.Timestamp("2026-10-21T00:00:00+09:00"),
            224000,
            225000,
            222000,
            224000,
            1,
            0,
            0,
            0,
            "",
            "",
        ]
        trade = backtest._enter_trade("005930/KRW", row, "long")
        exit_row = [pd.Timestamp("2026-10-22T00:00:00+09:00"), *row[1:]]
        expected = datetime(2026, 10, 22, tzinfo=UTC) + timedelta(minutes=detail_minutes)
        if detail_minutes:
            exit_row[0] = pd.Timestamp(expected)
        if forced:
            backtest.handle_left_open({"005930/KRW": [trade]}, {"005930/KRW": [exit_row]})
        else:
            backtest._exit_trade(trade, exit_row, 224000, 2, "validation")
            backtest._process_exit_order(
                trade.orders[-1], trade, exit_row[0].to_pydatetime(), exit_row, "005930/KRW"
            )
        assert trade.close_date_utc == expected
        assert trade.orders[-1].order_filled_date == expected
        assert trade.orders[-1].order_date == expected
        assert filled.call_args.kwargs["current_time"] == expected
        detail = expected + timedelta(minutes=5)
        assert exchange.candle_execution_time("1d", detail) == detail
    finally:
        Backtesting.cleanup()
        exchange.close()


def test_daily_detail_generator_runs_trades_on_actual_session_rows(kis_config, sources, mocker):
    kis_config.update(
        runmode=RunMode.BACKTEST,
        timeframe="1d",
        timeframe_detail="1m",
        stake_amount=500000,
        dry_run_wallet=1000000,
        max_open_trades=1,
    )
    exchange = Kis(kis_config)
    try:
        backtest = Backtesting(kis_config, exchange=exchange)
        backtest._set_strategy(backtest.strategylist[0])
        backtest.strategy.minimal_roi = {0: 1.0}
        open_time = datetime(2026, 10, 21, tzinfo=UTC)
        exit_time = open_time + timedelta(minutes=1)
        mocker.patch.object(
            backtest.strategy,
            "custom_exit",
            side_effect=lambda **kwargs: (
                "detail_validation" if kwargs["current_time"] >= exit_time else None
            ),
        )
        filled = mocker.patch.object(backtest.strategy, "order_filled")
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime([open_time, exit_time]),
                "open": 224000,
                "high": 225000,
                "low": 222000,
                "close": 224000,
                "volume": 1,
            }
        )
        backtest.detail_data = {"005930/KRW": frame}
        row = [
            pd.Timestamp("2026-10-21T00:00:00+09:00"),
            224000,
            225000,
            222000,
            224000,
            1,
            0,
            0,
            0,
            "",
            "",
        ]
        seen = []
        for when, pair, candle, last, direction in backtest.time_pair_generator(
            datetime(2026, 10, 20, tzinfo=SEOUL),
            datetime(2026, 10, 22, tzinfo=SEOUL),
            ["005930/KRW"],
            {"005930/KRW": [row]},
        ):
            seen.append(when)
            assert when == candle[0].to_pydatetime()
            backtest.backtest_loop(candle, pair, when, direction, not last)
        assert seen == [open_time, exit_time]
        assert [c.kwargs["current_time"] for c in filled.call_args_list] == seen
        trade = LocalTrade.bt_trades[-1]
        assert trade.open_date_utc == open_time and trade.close_date_utc == exit_time
        assert trade.orders[-1].order_filled_date == exit_time
    finally:
        Backtesting.cleanup()
        exchange.close()


def test_backtest_detail_data_rejects_a_missing_trading_minute(kis_config, sources, mocker):
    kis_config.update(runmode=RunMode.BACKTEST, timeframe="5m", timeframe_detail="1m")
    exchange = Kis(kis_config)
    try:
        backtest = Backtesting(kis_config, exchange=exchange)
        frame = pd.DataFrame(
            {"date": pd.to_datetime(["2026-10-22T00:00:00Z", "2026-10-22T00:02:00Z"])}
        )
        mocker.patch(
            "freqtrade.optimize.backtesting.history.load_data", return_value={"005930/KRW": frame}
        )
        with pytest.raises(OperationalException, match="session gap"):
            backtest._load_bt_data_detail()
    finally:
        Backtesting.cleanup()
        exchange.close()


def test_offline_warmup_counts_actual_bars_across_a_weekend(tmp_path):
    dates = pd.to_datetime(
        [
            "2026-10-16T06:18:00Z",
            "2026-10-16T06:19:00Z",
            "2026-10-16T06:30:00Z",
            "2026-10-19T00:00:00Z",
            "2026-10-19T00:01:00Z",
        ]
    )
    frame = pd.DataFrame(
        {"date": dates, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}
    )
    handler = get_datahandler(tmp_path, "feather")
    handler.ohlcv_store("005930/KRW", "1m", frame, candle_type=CandleType.SPOT)
    timerange = TimeRange("date", None, int(datetime(2026, 10, 19, tzinfo=UTC).timestamp()), 0)
    result = load_pair_history(
        "005930/KRW",
        "1m",
        tmp_path,
        timerange=timerange,
        startup_candles=3,
        startup_by_count=True,
        fill_up_missing=False,
        data_format="feather",
    )
    assert list(result["date"]) == list(dates)
    assert (
        Exchange.__dict__["estimate_fee_rate"](None, "BTC/USDT", "sell", 1, 100, NOW, None) is None
    )
