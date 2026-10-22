import copy
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import ccxt
import pandas as pd
import pytest

from freqtrade.data.dataprovider import DataProvider
from freqtrade.exchange import Kis
from freqtrade.exchange.kis_adapter import investor_flow as module
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.investor_flow import (
    InvestorFlowStore,
    KisInvestorFlow,
    amounts,
    enrich_investor_flow,
    observation,
)
from freqtrade.exchange.kis_adapter.rules import SEOUL
from tests.exchange.kis_adapter.conftest import SCHEDULE


CAPTURE = json.loads((Path(__file__).parents[2] / "testdata/kis/flow_contract.json").read_text())
WHEN = datetime(2026, 10, 22, 23, 15, tzinfo=SEOUL)
SYMBOL = CAPTURE["responses"]["rank_buy"]["output"][0]["mksc_shrn_iscd"] + "/KRW"


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(module, "dt_now", lambda: WHEN)
    client = KisClient(
        {"apiKey": "test-key", "secret": "test-secret", "options": {"environment": "live"}}
    )

    def get(path, tr, params):
        if path.endswith("foreign-institution-total"):
            key = "rank_buy" if params["FID_RANK_SORT_CLS_CODE"] == "0" else "rank_sell"
            return copy.deepcopy(CAPTURE["responses"][key]), ""
        assert path.endswith("investor-trade-by-stock-daily")
        return copy.deepcopy(CAPTURE["responses"]["daily_flow"]), ""

    client.get = MagicMock(side_effect=get)
    return KisInvestorFlow(client, {"sessions": {"2026-10-22": SCHEDULE}})


def test_captured_flow_units_unknown_dates_and_individual_scope(source):
    estimate, daily = source.collect([SYMBOL])
    raw = CAPTURE["responses"]["rank_buy"]["output"][0]
    assert estimate["amounts"]["foreign"] == int(raw["frgn_ntby_tr_pbmn"]) * 1_000_000
    assert estimate["amounts"]["individual"] is None
    assert estimate["reasons"]["individual"] == "individual_intraday_unavailable"
    assert estimate["business_date"] is None and estimate["source_updated_at"] is None
    assert estimate["basis"] == "broker_quantity_times_current_price"
    assert daily["business_date"] == "2026-10-22"
    assert daily["amounts"]["individual"] == 212488_000000
    assert len(daily["history"]) == 3
    assert source.client.get.call_count == 3


def test_missing_rank_and_api_error_never_become_zero(source):
    estimate, _ = source.collect(["ZZZZZZ/KRW"])
    assert estimate["availability"] == "not_in_ranked_response"
    assert all(value is None for value in estimate["amounts"].values())
    source.client.get.side_effect = ccxt.RateLimitExceeded("secret-server-message")
    records = source.collect([SYMBOL])
    assert all(record["availability"] == "request_failed:RateLimitExceeded" for record in records)
    assert "secret" not in json.dumps(records)
    assert all(value is None for record in records for value in record["amounts"].values())


def test_zero_negative_fractional_and_invalid_amounts():
    values, _ = amounts({"frgn_ntby_tr_pbmn": "0", "orgn_ntby_tr_pbmn": "-1.5"})
    assert values == {"foreign": 0, "institution": -1500000, "individual": None}
    for raw in ("NaN", "Infinity", True, "0.0000001", "99999999999999999999"):
        with pytest.raises(ccxt.ExchangeError):
            amounts({"frgn_ntby_tr_pbmn": raw})


def test_conflicting_ranked_rows_are_unavailable(source):
    row = copy.deepcopy(CAPTURE["responses"]["rank_buy"]["output"][0])
    changed = row | {"frgn_ntby_tr_pbmn": "999"}
    source.client.get.side_effect = [
        ({"output": [row]}, ""),
        ({"output": [changed]}, ""),
        (CAPTURE["responses"]["daily_flow"], ""),
    ]
    estimate, _ = source.collect([SYMBOL])
    assert estimate["availability"] == "conflicting_rank_rows"
    assert estimate["amounts"]["foreign"] is None


@pytest.mark.parametrize("hour,minute", [(9, 30), (15, 39)])
def test_current_day_flow_waits_for_verified_post_close(source, monkeypatch, hour, minute):
    monkeypatch.setattr(module, "dt_now", lambda: WHEN.replace(hour=hour, minute=minute))
    _, daily = source.collect([SYMBOL])
    assert daily["business_date"] == "2026-10-21"
    assert all(row["business_date"] != "2026-10-22" for row in daily["history"])


def test_missing_close_calendar_and_future_rows_fail_closed(source):
    source.data._sessions.clear()
    assert source.collect([SYMBOL])[1]["business_date"] == "2026-10-21"
    original = source.client.get.side_effect

    def future(path, tr, params):
        data, cont = original(path, tr, params)
        if "output2" in data:
            data["output2"][0]["stck_bsop_date"] = "20261023"
        return data, cont

    source.client.get.side_effect = future
    assert source.collect([SYMBOL])[1]["availability"] == "request_failed:ExchangeError"


def test_paper_collection_is_rejected_before_authentication():
    client = KisClient({"apiKey": "test", "secret": "secret"})
    with pytest.raises(ccxt.NotSupported, match="production read-only"):
        KisInvestorFlow(client, {})
    assert client._token is None


def test_store_deduplication_revision_restart_and_clock_rollback(tmp_path, source):
    path = tmp_path / "flow.sqlite"
    records = source.collect([SYMBOL])
    store = InvestorFlowStore(path)
    assert store.save(records) == 2
    repeated = copy.deepcopy(records)
    for record in repeated:
        record["observed_at"] += 1000
    assert store.save(repeated) == 0
    revision = copy.deepcopy(repeated[0])
    revision["amounts"]["foreign"] += 1
    assert store.save([revision]) == 1
    restored_value = copy.deepcopy(records[0])
    restored_value["observed_at"] += 2000
    assert store.save([restored_value]) == 1
    store.close()
    store = InvestorFlowStore(path, read_only=True)
    assert store.latest(SYMBOL, "estimate", records[0]["observed_at"] - 1) is None
    assert (
        store.latest(SYMBOL, "estimate", revision["observed_at"])["amounts"] == revision["amounts"]
    )
    assert (
        store.latest(SYMBOL, "estimate", restored_value["observed_at"])["amounts"]
        == records[0]["amounts"]
    )
    store.close()
    store = InvestorFlowStore(path)
    with pytest.raises(ccxt.ExchangeError, match="backwards"):
        store.save([records[0]])
    assert store.db.execute("SELECT count(*) FROM kis_investor_observations").fetchone()[0] == 4
    store.close()
    assert path.stat().st_mode & 0o777 == 0o600


def test_strategy_join_uses_observation_time_preserves_zero_and_staleness(tmp_path):
    path = tmp_path / "flow.sqlite"
    store = InvestorFlowStore(path)
    first = WHEN.replace(hour=10, minute=0, second=30)
    record = observation(
        SYMBOL, "estimate", first, row={"frgn_ntby_tr_pbmn": "0", "orgn_ntby_tr_pbmn": "-2"}
    )
    store.save([record])
    store.close()
    dates = [
        first.replace(second=0) - timedelta(minutes=1),
        first.replace(second=0),
        first.replace(hour=14, second=0),
        first.replace(day=23, second=0),
    ]
    frame = pd.DataFrame({"date": pd.to_datetime(dates, utc=True)})
    result = enrich_investor_flow(
        frame, SYMBOL, "1m", path, lambda tf, stamp: stamp + timedelta(minutes=1)
    )
    assert result["kis_estimate_foreign_krw"].isna().tolist() == [True, False, True, True]
    assert result.iloc[1]["kis_estimate_foreign_krw"] == 0
    assert result.iloc[1]["kis_estimate_institution_krw"] == -2000000
    assert result["kis_estimate_individual_krw"].isna().all()
    assert result.iloc[1]["kis_estimate_individual_reason"] == "individual_intraday_unavailable"
    assert result.iloc[2]["kis_estimate_status"] == "stale_observation"
    assert "kis_estimate_foreign_krw" not in frame


def test_missing_archive_and_unknown_decision_are_explicit(tmp_path):
    frame = pd.DataFrame({"date": [pd.Timestamp(WHEN)]})
    path = tmp_path / "absent.sqlite"
    result = enrich_investor_flow(frame, SYMBOL, "1m", path, lambda tf, stamp: None)
    assert result.iloc[0]["kis_daily_status"] == "unknown_candle_publication"
    assert result["kis_daily_individual_krw"].isna().all()
    assert not path.exists()


def test_failed_atomic_batch_and_error_observations_do_not_reuse_old_values(tmp_path):
    path = tmp_path / "flow.sqlite"
    store = InvestorFlowStore(path)
    original = observation(SYMBOL, "estimate", WHEN, row={"frgn_ntby_tr_pbmn": "2"})
    store.save([original])
    changed = observation(
        SYMBOL, "estimate", WHEN + timedelta(seconds=1), row={"frgn_ntby_tr_pbmn": "3"}
    )
    older = original | {"observed_at": original["observed_at"] - 1}
    with pytest.raises(ccxt.ExchangeError, match="backwards"):
        store.save([changed, older])
    assert store.latest(SYMBOL, "estimate", 2**63 - 1) == original
    failed = observation(
        SYMBOL, "estimate", WHEN + timedelta(seconds=2), reason="request_failed:RateLimitExceeded"
    )
    store.save([failed])
    store.close()
    frame = pd.DataFrame({"date": [pd.Timestamp(WHEN + timedelta(seconds=3))]})
    result = enrich_investor_flow(frame, SYMBOL, "1m", path, lambda tf, stamp: stamp)
    assert result["kis_estimate_foreign_krw"].isna().all()
    assert result.iloc[0]["kis_estimate_foreign_reason"] == "request_failed:RateLimitExceeded"


def test_dataprovider_reads_same_archive_after_restart(kis_config, sources, tmp_path):
    path = tmp_path / "flow.sqlite"
    store = InvestorFlowStore(path)
    when = datetime(2026, 10, 22, 9, 0, 30, tzinfo=SEOUL)
    store.save([observation("005930/KRW", "estimate", when, row={"frgn_ntby_tr_pbmn": "1"})])
    store.close()
    kis_config["exchange"]["kis"]["flow_database"] = str(path)
    exchange = Kis(kis_config)
    try:
        frame = pd.DataFrame({"date": [pd.Timestamp(when.replace(second=0))]})
        for _ in range(2):
            result = DataProvider(kis_config, exchange).kis_investor_flow(frame, "005930/KRW")
            assert result.iloc[0]["kis_estimate_foreign_krw"] == 1000000
    finally:
        exchange.close()


def test_collector_cli_once_persists_and_redacts_errors(tmp_path, source, monkeypatch, capsys):
    config = json.loads(Path("config_examples/config_kis.example.json").read_text())
    config.update(user_data_dir=str(tmp_path))
    config["exchange"].update(api_key="test-key", secret="test-secret", pair_whitelist=[SYMBOL])
    config["exchange"]["kis"].update(environment="live", sessions={"2026-10-22": SCHEDULE})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(module, "KisClient", lambda conf: source.client)
    database = tmp_path / "flow.sqlite"
    assert module.main(["--config", str(path), "--database", str(database)]) == 0
    output = capsys.readouterr().out
    assert '"broker_mutations": 0' in output and "test-secret" not in output
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM kis_investor_observations").fetchone()[0] == 2
