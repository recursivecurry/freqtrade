import copy
import json
import logging
from datetime import timedelta
from unittest.mock import MagicMock

import ccxt
import pandas as pd
import pytest
from filelock import FileLock

from freqtrade.data.dataprovider import DataProvider
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.kis_adapter import model_flow as module
from freqtrade.exchange.kis_adapter.flow_model import save_model, train_model
from freqtrade.exchange.kis_adapter.investor_flow import InvestorFlowStore, enrich_investor_flow
from freqtrade.exchange.kis_adapter.model_flow import collect_predictions, enrich_model_flow
from tests.exchange.kis_adapter.conftest import SCHEDULE
from tests.exchange.kis_adapter.test_flow_model import NOW, history, snapshot


@pytest.fixture
def runtime(monkeypatch):
    now = NOW.replace(hour=10)
    model = train_model(history(), NOW)
    source = MagicMock()
    source.intraday.return_value = snapshot(model, now)
    monkeypatch.setattr(module, "dt_now", lambda: now)
    return model, source, now


def test_research_prediction_and_failure_history_preserve_formal_data(runtime, tmp_path):
    model, source, _now = runtime
    path = tmp_path / "flow.sqlite"
    records = collect_predictions(source, [model])
    assert records[0]["availability"] == "research_only"
    assert records[0]["quantities"]["foreign"] > 0
    assert records[0]["input_snapshot"]["unit"] == "shares"
    store = InvestorFlowStore(path)
    store.save(records)
    source.intraday.side_effect = ccxt.RateLimitExceeded("private-secret")
    failed = collect_predictions(source, [model])
    failed[0]["observed_at"] += 1000
    assert all(v is None for v in failed[0]["quantities"].values())
    assert "private-secret" not in json.dumps(failed)
    store.save(failed)
    assert store.latest(model["symbol"], "model", failed[0]["observed_at"])[
        "availability"
    ].startswith("unavailable:")
    assert store.latest(model["symbol"], "estimate", failed[0]["observed_at"]) is None
    store.close()


def test_asof_display_distinguishes_research_stale_and_uncollected(runtime, tmp_path):
    model, source, now = runtime
    path = tmp_path / "flow.sqlite"
    store = InvestorFlowStore(path)
    store.save(collect_predictions(source, [model]))
    store.close()
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [now - timedelta(seconds=1), now, now + timedelta(minutes=6)], utc=True
            )
        }
    )
    result = enrich_model_flow(frame, model["symbol"], "1m", path, lambda tf, when: when)
    assert result["kis_model_status"].tolist() == [
        "not_collected",
        "research_only",
        "stale_observation",
    ]
    assert result["kis_model_foreign_shares"].isna().all()
    assert result.iloc[1]["kis_model_label"] != "공식 제공 가집계 · 잠정치"
    assert result.iloc[1]["kis_model_model_id"] == model["model_id"]
    official = enrich_investor_flow(frame, model["symbol"], "1m", path, lambda tf, when: when)
    assert official["kis_estimate_foreign_krw"].isna().all()
    assert official.iloc[0]["kis_estimate_label"] == "공식 제공 가집계 · 잠정치"
    assert official.iloc[0]["kis_daily_label"] == "공식 일별 수급 · 정정 가능"


def test_missing_archive_and_clock_are_explicit(runtime, tmp_path):
    model, _, now = runtime
    path = tmp_path / "missing.sqlite"
    frame = pd.DataFrame({"date": pd.to_datetime([now], utc=True)})
    result = enrich_model_flow(frame, model["symbol"], "1m", path, lambda tf, when: None)
    assert result.iloc[0]["kis_model_status"] == "unknown_candle_publication"
    assert not path.exists()
    with pytest.raises(ValueError):
        enrich_model_flow(frame, model["symbol"], "1m", path, lambda tf, when: when, 6)


def configured(tmp_path, model):
    return {
        "name": "kis",
        "api_key": "test",
        "secret": "test-secret",
        "pair_whitelist": [model["symbol"]],
        "kis": {
            "environment": "live",
            "sessions": {NOW.date().isoformat(): copy.deepcopy(SCHEDULE)},
        },
    }


def test_collect_and_inspect_cli_once_with_lock_and_safe_errors(
    runtime, tmp_path, monkeypatch, capsys
):
    model, source, now = runtime
    model_path = tmp_path / "model.json"
    save_model(model, model_path)
    monkeypatch.setattr(module, "_configured", lambda args: configured(tmp_path, model))
    monkeypatch.setattr(module, "KisModelInputs", lambda *args: source)
    path = tmp_path / "flow.sqlite"
    args = ["collect", "--config", "unused", "--model", str(model_path), "--database", str(path)]
    prior_logging = logging.root.manager.disable
    assert module.main(args) == 0
    assert logging.root.manager.disable == prior_logging
    assert '"broker_mutations": 0' in capsys.readouterr().out
    with FileLock(str(path) + ".model.lock", timeout=0):
        assert module.main(args) == 1
    assert (
        module.main(
            [
                "inspect",
                "--database",
                str(path),
                "--symbol",
                model["symbol"],
                "--asof",
                now.isoformat(),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"kind": "model"' in output and "test-secret" not in output


def test_train_cli_refuses_intraday_and_persists_outside_session(
    runtime, tmp_path, monkeypatch, capsys
):
    model, _, now = runtime
    monkeypatch.setattr(module, "_configured", lambda args: configured(tmp_path, model))
    history_path = tmp_path / "history.json"
    history_path.write_text(json.dumps(history()))
    output = tmp_path / "model.json"
    args = ["train", "--config", "unused", "--history", str(history_path), "--output", str(output)]
    assert module.main(args) == 1
    assert not output.exists()
    monkeypatch.setattr(module, "dt_now", lambda: now.replace(hour=8))
    assert module.main(args) == 0
    assert json.loads(output.read_text())["intraday_validation"] == "pending"


def test_dataprovider_model_namespace_requires_kis_clock(runtime, tmp_path, kis_config):
    model, source, now = runtime
    path = tmp_path / "flow.sqlite"
    store = InvestorFlowStore(path)
    store.save(collect_predictions(source, [model]))
    store.close()
    kis_config["exchange"]["kis"]["flow_database"] = str(path)
    exchange = MagicMock()
    exchange.candle_publication_time.side_effect = lambda tf, when: when
    frame = pd.DataFrame({"date": pd.to_datetime([now], utc=True)})
    result = DataProvider(kis_config, exchange).kis_model_flow(frame, model["symbol"])
    assert result.iloc[0]["kis_model_status"] == "research_only"
    assert result["kis_model_foreign_shares"].isna().all()
    with pytest.raises(OperationalException):
        DataProvider(kis_config, None).kis_model_flow(frame, model["symbol"])
