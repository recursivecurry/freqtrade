import copy
import json
from datetime import timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from freqtrade.exchange.kis_adapter import model_flow as runtime
from freqtrade.exchange.kis_adapter.flow_model import predict_model, train_model
from freqtrade.exchange.kis_adapter.flow_validation import (
    PROTOCOL,
    evaluate_intraday,
    validation_allows,
)
from freqtrade.exchange.kis_adapter.investor_flow import InvestorFlowStore
from freqtrade.exchange.kis_adapter.model_flow import collect_predictions, enrich_model_flow
from freqtrade.exchange.kis_adapter.rules import Session
from freqtrade.util import dt_ts
from tests.exchange.kis_adapter.conftest import SCHEDULE
from tests.exchange.kis_adapter.test_flow_model import NOW, history, snapshot


def model_at(now):
    data = history()
    offset = now.date() - NOW.date()
    data["available_at"] = dt_ts(now - timedelta(seconds=10))
    for row in data["samples"]:
        row["business_date"] = (pd.Timestamp(row["business_date"]).date() + offset).isoformat()
    return train_model(data, now, dominant_members=1, alpha=0.01)


@pytest.fixture
def evidence():
    models, pairs, sessions = [], [], {}
    day = NOW - timedelta(days=1)
    while len(models) < 10:
        if day.weekday() < 5:
            model = model_at(day)
            models.append(model)
            sessions[day.date().isoformat()] = copy.deepcopy(SCHEDULE)
            session = Session.from_config(day.date(), SCHEDULE)
            for hour, minute in [
                (9, 10),
                (9, 20),
                (9, 30),
                (10, 10),
                (11, 10),
                (12, 10),
                (14, 10),
                (14, 20),
                (14, 30),
            ]:
                when = day.replace(hour=hour, minute=minute)
                inputs = snapshot(model, when)
                net = (minute - 15 + (hour - 9) * 7) * 100
                inputs["members"] = {"00003": net}
                prediction = {
                    "symbol": model["symbol"],
                    "kind": "model",
                    "market": "KRX",
                    "unit": "shares",
                    "business_date": day.date().isoformat(),
                    "model_id": model["model_id"],
                    "input_snapshot": inputs,
                    "observed_at": dt_ts(when),
                    "inference_at": dt_ts(when),
                    "quantities": predict_model(model, inputs, when),
                }
                reference = {
                    "symbol": model["symbol"],
                    "market": "KRX",
                    "unit": "shares",
                    "business_date": day.date().isoformat(),
                    "source": "synthetic independent fixture",
                    "asof": inputs["available_at"],
                    "available_at": dt_ts(when),
                    "cumulative_from": dt_ts(session.opening),
                    "response_sha256": "a" * 64,
                    "frequency": "intraday",
                    "basis": "reported_cumulative_net_quantity",
                    "targets": {"foreign": 2 * net, "institution": -net, "individual": -3 * net},
                }
                pairs.append({"prediction": prediction, "reference": reference})
        day -= timedelta(days=1)
    return {
        "protocol": PROTOCOL,
        "symbol": models[0]["symbol"],
        "recipe_id": models[0]["recipe_id"],
        "protocol_registered_at": dt_ts(day - timedelta(days=1)),
        "models": models,
        "pairs": pairs,
        "sessions": sessions,
    }


def test_rolling_models_meet_preset_sample_and_metric_gates(evidence):
    report = evaluate_intraday(evidence, NOW)
    assert report["eligible"] and report["dates"] == 10
    assert all(w["pairs"] == 30 and w["dates"] == 10 for w in report["windows"].values())
    assert report["alignment"] == "receipt_time_with_unknown_source_latency"
    model = model_at(NOW)
    assert validation_allows(report, model, NOW)
    assert not validation_allows(report, model, NOW - timedelta(seconds=1))
    assert not validation_allows(report, model, NOW + timedelta(days=15))


@pytest.mark.parametrize(
    "change",
    [
        "future_reference",
        "postclose_as_morning",
        "other_market",
        "missing_target",
        "replayed_prediction",
        "unregistered",
        "duplicate",
        "wrong_recipe",
    ],
)
def test_bad_reference_or_prediction_cannot_activate(evidence, change):
    pair = evidence["pairs"][0]
    if change == "future_reference":
        pair["reference"]["available_at"] = dt_ts(NOW + timedelta(days=1))
    elif change == "postclose_as_morning":
        pair["reference"]["frequency"] = "daily"
    elif change == "other_market":
        pair["reference"]["market"] = "UN"
    elif change == "missing_target":
        pair["reference"]["targets"].pop("individual")
    elif change == "replayed_prediction":
        pair["prediction"]["quantities"]["foreign"] += 1
    elif change == "unregistered":
        evidence["protocol_registered_at"] = dt_ts(NOW)
    elif change == "duplicate":
        evidence["pairs"].append(copy.deepcopy(pair))
    else:
        evidence["recipe_id"] = "0" * 64
    with pytest.raises(ValueError):
        evaluate_intraday(evidence, NOW)


def test_insufficient_or_failed_metrics_and_stale_reference_remain_research(evidence):
    reduced = copy.deepcopy(evidence)
    reduced["pairs"] = reduced["pairs"][:9]
    assert not evaluate_intraday(reduced, NOW)["eligible"]
    poor = copy.deepcopy(evidence)
    for pair in poor["pairs"]:
        pair["reference"]["targets"] = {k: -v for k, v in pair["reference"]["targets"].items()}
    assert not evaluate_intraday(poor, NOW)["eligible"]
    assert not evaluate_intraday(evidence, NOW + timedelta(days=16))["eligible"]


def test_validation_expiry_and_available_time_gate_strategy_values(evidence, tmp_path, monkeypatch):
    report = evaluate_intraday(evidence, NOW)
    model = model_at(NOW)
    now = NOW.replace(hour=10)
    monkeypatch.setattr(runtime, "dt_now", lambda: now)
    source = MagicMock()
    source.intraday.return_value = snapshot(model, now)
    record = collect_predictions(source, [model], {model["symbol"]: report})[0]
    assert record["availability"] == "validated"
    path = tmp_path / "flow.sqlite"
    store = InvestorFlowStore(path)
    store.save([record])
    store.close()
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [now - timedelta(seconds=1), now, now + timedelta(minutes=6)], utc=True
            )
        }
    )
    result = enrich_model_flow(frame, model["symbol"], "1m", path, lambda tf, when: when)
    assert result["kis_model_foreign_shares"].isna().tolist() == [True, False, True]
    assert result.iloc[1]["kis_model_label"] == "장중 추정 수급 · 모델 추정치"
    late = copy.deepcopy(report)
    late["available_at"] = dt_ts(now + timedelta(minutes=1))
    assert (
        collect_predictions(source, [model], {model["symbol"]: late})[0]["availability"]
        == "research_only"
    )


def test_validation_cli_recomputes_evidence_without_credentials(
    evidence, tmp_path, monkeypatch, capsys
):
    path, output = tmp_path / "evidence.json", tmp_path / "report.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setattr(runtime, "dt_now", lambda: NOW)
    assert runtime.main(["validate", "--evidence", str(path), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["eligible"]
    capsys.readouterr()


def test_delayed_batch_publication_never_refreshes_old_inputs(evidence, tmp_path, monkeypatch):
    model = model_at(NOW)
    now = NOW.replace(hour=10)
    report = evaluate_intraday(evidence, NOW)
    monkeypatch.setattr(runtime, "dt_now", lambda: now)
    source = MagicMock()
    source.intraday.return_value = snapshot(model, now)
    record = collect_predictions(source, [model], {model["symbol"]: report})[0]
    published = now + timedelta(minutes=6)
    record["observed_at"] = dt_ts(published)
    path = tmp_path / "late.sqlite"
    store = InvestorFlowStore(path)
    store.save([record])
    store.close()
    frame = pd.DataFrame({"date": pd.to_datetime([now, published], utc=True)})
    result = enrich_model_flow(frame, model["symbol"], "1m", path, lambda tf, when: when)
    assert result["kis_model_status"].tolist() == ["not_collected", "stale_observation"]
    assert result["kis_model_foreign_shares"].isna().all()
