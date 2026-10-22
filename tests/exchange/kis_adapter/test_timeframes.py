from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import ccxt
import pandas as pd
import pytest

from freqtrade.data.dataprovider import DataProvider
from freqtrade.enums import CandleType, RunMode
from freqtrade.exchange import Kis
from freqtrade.exchange.kis_adapter import market_data as data_module
from freqtrade.exchange.kis_adapter.market_data import complete_minutes
from freqtrade.exchange.kis_adapter.rules import (
    SEOUL,
    TIMEFRAMES,
    Session,
    broker_timestamp,
    intraday_windows,
    period_bounds,
)
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.strategy.informative_decorator import (
    InformativeData,
    _create_and_merge_informative_pair,
)
from freqtrade.strategy.strategy_helper import merge_informative_pair
from tests.exchange.kis_adapter.conftest import SCHEDULE
from tests.exchange.kis_adapter.test_market_data import (
    NORMAL,
    FrozenDateTime,
    client_and_data,
)


@pytest.mark.parametrize("timeframe,count", [("15m", 27), ("30m", 14), ("1h", 8)])
def test_larger_intraday_bars_preserve_captured_volume(monkeypatch, timeframe, count):
    monkeypatch.setattr(data_module, "datetime", FrozenDateTime)
    _, data = client_and_data()
    since = broker_timestamp("20261021", "090000")
    bars = data.fetch_ohlcv("005930", timeframe, since, 1000)
    assert len(bars) == count
    assert sum(row[5] for row in bars) == 16086411
    assert bars[0][0] == since
    assert bars[-1][0] == broker_timestamp("20261021", "153000")
    session = data.session(date(2026, 10, 21))
    assert [r[0] for r in bars] == [start for start, _ in intraday_windows(session, timeframe)]


@pytest.mark.parametrize("timeframe", ["15m", "30m", "1h"])
def test_exceptional_opening_and_short_final_bar(timeframe):
    session = Session.from_config(
        date(2026, 10, 22), NORMAL | {"open": "10:15", "auction": "16:35", "close": "16:45"}
    )
    rows = [[stamp, 100, 110, 90, 105, 1] for stamp in session.minute_starts()]
    windows = intraday_windows(session, timeframe)
    now = int(session.closing.timestamp() * 1000) + 60000
    bars = complete_minutes(rows, session, now, timeframe)
    assert len(bars) == len(windows)
    assert sum(row[5] for row in bars) == len(rows)
    assert bars[0][0] == int(session.opening.timestamp() * 1000)
    assert windows[-2][1] == int(session.auction.timestamp() * 1000)
    before = windows[0][1] - 1
    partial = [row for row in rows if row[0] + 60000 <= before]
    assert complete_minutes(partial, session, before, timeframe) == []


def calendar(first, last):
    return {
        (day := first + timedelta(days=i)).isoformat(): SCHEDULE if day.weekday() < 5 else None
        for i in range((last - first).days + 1)
    }


def daily_source(monkeypatch, first, today):
    monkeypatch.setattr(data_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        FrozenDateTime, "current", datetime.combine(today, datetime.min.time(), SEOUL)
    )
    client, data = client_and_data()
    data._sessions = calendar(first, today)

    def get(path, tr, params):
        assert params["FID_PERIOD_DIV_CODE"] == "D"
        days = [
            day
            for day, session in data._sessions.items()
            if session
            and params["FID_INPUT_DATE_1"] <= day.replace("-", "") <= params["FID_INPUT_DATE_2"]
        ]
        rows = [
            {
                "stck_bsop_date": day.replace("-", ""),
                "stck_oprc": "100",
                "stck_hgpr": "120",
                "stck_lwpr": "90",
                "stck_clpr": "110",
                "acml_vol": "10",
                "mod_yn": "N",
                "flng_cls_code": "00",
                "prtt_rate": "0",
            }
            for day in sorted(days, reverse=True)[:100]
        ]
        return {"output2": rows}, ""

    client.get.side_effect = get
    return client, data


@pytest.mark.parametrize(
    "first,today,timeframe,labels",
    [
        (date(2026, 10, 1), date(2026, 11, 10), "1M", ["20261001"]),
        (date(2026, 10, 19), date(2026, 11, 3), "1w", ["20261019", "20261026"]),
        (date(2025, 12, 1), date(2026, 2, 1), "1M", ["20251201", "20260101"]),
    ],
)
def test_period_bars_are_calendar_aggregates(monkeypatch, first, today, timeframe, labels):
    _, data = daily_source(monkeypatch, first, today)
    bars = data.fetch_ohlcv("005930", timeframe, broker_timestamp(first.strftime("%Y%m%d")), 100)
    assert [row[0] for row in bars] == [broker_timestamp(label) for label in labels]
    for row, label in zip(bars, labels, strict=True):
        start, end = period_bounds(datetime.strptime(label, "%Y%m%d").date(), timeframe)
        count = sum(
            bool(record)
            for day, record in data._sessions.items()
            if start.isoformat() <= day < end.isoformat()
        )
        assert row[1:] == [100, 120, 90, 110, count * 10]


def test_period_history_more_than_one_year_and_missing_calendar(monkeypatch):
    first, today = date(2024, 1, 1), date(2026, 10, 22)
    client, data = daily_source(monkeypatch, first, today)
    bars = data.fetch_ohlcv("005930", "1M", broker_timestamp("20240101"), 100)
    assert len(bars) == 33
    assert client.get.call_count > 1
    data._sessions.pop("2024-01-06")
    with pytest.raises(ccxt.ExchangeNotAvailable, match="calendar"):
        data.fetch_ohlcv("005930", "1M", broker_timestamp("20240101"), 100)


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_all_timeframes_load_through_freqtrade(kis_config, sources, timeframe):
    kis_config["timeframe"] = timeframe
    exchange = Kis(kis_config)
    try:
        assert timeframe in exchange.timeframes
    finally:
        exchange.close()


def test_monthly_publication_holiday_execution_and_runtime_refresh(kis_config, sources, mocker):
    sessions = calendar(date(2026, 9, 1), date(2026, 11, 5))
    sessions["2026-11-02"] = None
    kis_config["exchange"]["kis"]["sessions"] = sessions
    exchange = Kis(kis_config)
    try:
        label = datetime(2026, 10, 1, tzinfo=SEOUL)
        now = datetime(2026, 11, 3, 9, 1, tzinfo=SEOUL)
        assert exchange.candle_close_time("1M", label, now) == now.replace(minute=0)
        assert exchange.candle_execution_time(
            "1M", datetime(2026, 11, 1, tzinfo=SEOUL)
        ) == now.replace(minute=0).astimezone(UTC)
        mocker.patch("freqtrade.exchange.kis.dt_now", return_value=now)
        assert exchange._now_is_time_to_refresh("005930/KRW", "1M", CandleType.SPOT)
        exchange._pairs_last_refresh_time[("005930/KRW", "1M", CandleType.SPOT)] = int(
            label.timestamp() * 1000
        )
        assert not exchange._now_is_time_to_refresh("005930/KRW", "1M", CandleType.SPOT)
        exchange.validate_candle_data(
            "005930/KRW", "1M", pd.DataFrame({"date": [pd.Timestamp(label)]})
        )
        assert exchange.candle_end_time("1M", label) == datetime(
            2026, 11, 1, tzinfo=SEOUL
        ).astimezone(UTC)
    finally:
        exchange.close()


def test_monthly_backtest_visits_february_and_march_calendar_rows(kis_config, sources):
    kis_config.update(runmode=RunMode.BACKTEST, timeframe="1M")
    kis_config["exchange"]["kis"]["sessions"] = calendar(date(2026, 1, 1), date(2026, 4, 1))
    exchange = Kis(kis_config)
    try:
        backtest = Backtesting(kis_config, exchange=exchange)
        backtest._set_strategy(backtest.strategylist[0])
        dates = [datetime(2026, month, 1, tzinfo=SEOUL) for month in (2, 3)]
        rows = [[pd.Timestamp(day), 100, 100, 100, 100, 0, 0, 0, 0, "", ""] for day in dates]
        result = list(
            backtest.time_pair_generator(
                datetime(2026, 1, 1, tzinfo=SEOUL), dates[-1], ["005930/KRW"], {"005930/KRW": rows}
            )
        )
        assert [row[2][0].to_pydatetime() for row in result] == dates
        assert [row[0] for row in result] == [
            exchange.candle_execution_time("1M", day) for day in dates
        ]
    finally:
        Backtesting.cleanup()
        exchange.close()


@pytest.mark.parametrize(
    "timeframe,known,unfinished,decision",
    [
        ("1M", "2026-09-01", "2026-10-01", "2026-10-02"),
        ("1w", "2026-10-12", "2026-10-19", "2026-10-20"),
    ],
)
def test_informative_slices_do_not_publish_unfinished_kst_periods(
    kis_config, sources, mocker, timeframe, known, unfinished, decision
):
    kis_config.update(runmode=RunMode.BACKTEST, timeframe="1h")
    kis_config["exchange"]["kis"]["sessions"] = calendar(date(2026, 9, 1), date(2026, 11, 5))
    exchange = Kis(kis_config)
    try:
        dp = DataProvider(kis_config, exchange)
        dates = [pd.Timestamp(day, tz=SEOUL).tz_convert(UTC) for day in (known, unfinished)]
        mocker.patch.object(
            dp, "historic_ohlcv", return_value=pd.DataFrame({"date": dates, "close": [100, 999]})
        )
        dp._set_dataframe_max_date(datetime.fromisoformat(decision + "T09:00:00+09:00"))
        result = dp.get_pair_dataframe("005930/KRW", timeframe)
        assert result["close"].tolist() == [100]
    finally:
        exchange.close()


@pytest.mark.parametrize("decorator", [False, True])
def test_informative_merges_wait_until_actual_period_publication(
    kis_config, sources, mocker, decorator
):
    kis_config["exchange"]["kis"]["sessions"] = calendar(date(2026, 9, 1), date(2026, 11, 5))
    exchange = Kis(kis_config)
    try:
        dp = DataProvider(kis_config, exchange)
        base = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    [
                        day + "T00:00:00+09:00"
                        for day in ("2026-10-01", "2026-10-02", "2026-10-30", "2026-11-02")
                    ],
                    utc=True,
                )
            }
        )
        informative = pd.DataFrame(
            {"date": [pd.Timestamp("2026-10-01T00:00:00+09:00").tz_convert(UTC)], "close": [999]}
        )
        informative.attrs["kis_session_clock"] = True
        if decorator:
            mocker.patch.object(dp, "get_pair_dataframe", return_value=informative)
            strategy = SimpleNamespace(config=kis_config, dp=dp, timeframe="1d")
            result = _create_and_merge_informative_pair(
                strategy,
                base,
                {"pair": "005930/KRW"},
                InformativeData("", "1M", None, True, CandleType.SPOT, True),
                lambda self, frame, metadata: frame,
            )
        else:
            with pytest.raises(ValueError, match="publication_time"):
                merge_informative_pair(base, informative, "1d", "1M")
            result = merge_informative_pair(
                base, informative, "1d", "1M", publication_time=dp.candle_publication_time
            )
        assert result["close_1M"].isna().tolist() == [True, True, False, False]
        assert result["close_1M"].dropna().tolist() == [999, 999]
    finally:
        exchange.close()


@pytest.mark.parametrize("missing", ["1h", "1M", "both"])
@pytest.mark.parametrize("ffill", [False, True])
def test_informative_merge_unknown_publication_times_remain_unavailable(missing, ffill):
    stamp = pd.Timestamp("2026-10-02T09:00:00+09:00").tz_convert(UTC)
    base = pd.DataFrame({"date": [stamp]})
    informative = pd.DataFrame({"date": [stamp - pd.Timedelta(days=1)], "close": [999]})

    def publication(timeframe, when):
        return None if missing in (timeframe, "both") else when

    result = merge_informative_pair(
        base, informative, "1h", "1M", ffill=ffill, publication_time=publication
    )
    assert len(result) == 1 and result["close_1M"].isna().all()
