import copy
import json
import os
from datetime import datetime, timedelta

import ccxt
import numpy as np
import pytest

from freqtrade.exchange.kis_adapter.flow_model import (
    fingerprint,
    load_model,
    metrics,
    predict_model,
    save_model,
    train_model,
    validate_model,
)
from freqtrade.exchange.kis_adapter.rules import SEOUL
from freqtrade.util import dt_ts


NOW = datetime(2026, 10, 22, 8, tzinfo=SEOUL)


def history():
    samples = []
    for i in range(40):
        net, program = (i % 9 - 4) * 100, (i % 5 - 2) * 10
        samples.append(
            {
                "business_date": (NOW.date() - timedelta(days=40 - i)).isoformat(),
                "members": {"00003": net, "00050": i},
                "activity": {"00003": 1000, "00050": 100},
                "program": program,
                "targets": {
                    "foreign": 2 * net + program,
                    "institution": -net + 2 * program,
                    "individual": -3 * net - program,
                },
            }
        )
    return {
        "symbol": "005930/KRW",
        "market": "KRX",
        "unit": "shares",
        "samples": samples,
        "available_at": dt_ts(NOW - timedelta(seconds=10)),
        "sources": [],
    }


@pytest.fixture
def model():
    return train_model(history(), NOW, dominant_members=1, alpha=0.01)


def snapshot(model, now):
    return {
        "symbol": model["symbol"],
        "market": "KRX",
        "unit": "shares",
        "business_date": now.date().isoformat(),
        "members": {m: 100 for m in model["members"]},
        "program": 0,
        "collection_started_at": dt_ts(now - timedelta(seconds=20)),
        "available_at": dt_ts(now - timedelta(seconds=10)),
    }


def test_training_walk_forward_contract_and_signal(model):
    assert model["targets"] == ["foreign", "institution", "individual"]
    assert model["daily_gate_passed"]
    assert model["intraday_validation"] == "pending"
    assert model["fit_intercept"] is False
    assert len(model["folds"]) == 10
    assert all(f["train_end_date"] < f["business_date"] for f in model["folds"])
    assert model["daily_metrics"]["foreign_minus_individual"]["r2"] > 0.99
    assert model["model_id"] == fingerprint({k: v for k, v in model.items() if k != "model_id"})


def test_prediction_preserves_independent_targets_and_zero_intercept(model):
    now = NOW.replace(hour=10)
    inputs = snapshot(model, now)
    result = predict_model(model, inputs, now)
    assert result["foreign"] == pytest.approx(200, rel=0.01)
    assert result["institution"] == pytest.approx(-100, rel=0.01)
    assert result["individual"] == pytest.approx(-300, rel=0.01)
    inputs["members"] = dict.fromkeys(model["members"], 0)
    assert set(predict_model(model, inputs, now).values()) == {0.0}


def test_member_selection_uses_only_preceding_fold():
    data = history()
    for sample in data["samples"][30:]:
        sample["activity"]["00050"] = 10**9
    result = train_model(data, NOW, dominant_members=1)
    assert result["folds"][0]["members"] == ["00003"]
    assert result["members"] == ["00050"]


def test_json_round_trip_and_no_overwrite(model, tmp_path):
    path = tmp_path / "model.json"
    save_model(model, path)
    assert load_model(path) == model
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_model(model, path)
    modified = json.loads(path.read_text())
    modified["coefficients"][0][0] += 1
    path.write_text(json.dumps(modified))
    with pytest.raises(ValueError, match="artifact"):
        load_model(path)


@pytest.mark.parametrize(
    "change",
    [
        {"scale": [0, 1]},
        {"coefficients": [[1, 2, 3]]},
        {"targets": ["foreign"]},
        {"market": "UN"},
        {"fit_intercept": True},
        {"intraday_validation": "approved"},
        {"history_available_at": dt_ts(NOW) + 1},
        {"train_end_date": "2026-10-22"},
    ],
)
def test_malformed_even_rehashed_artifacts_fail(model, change):
    value = copy.deepcopy(model) | change
    value["model_id"] = fingerprint({k: v for k, v in value.items() if k != "model_id"})
    with pytest.raises(ValueError, match="artifact"):
        validate_model(value)


@pytest.mark.parametrize(
    "mutation", ["short", "duplicate", "future", "missing", "nonfinite", "provenance"]
)
def test_invalid_training_data_fail(mutation):
    data = history()
    if mutation == "short":
        data["samples"] = data["samples"][:29]
    elif mutation == "duplicate":
        data["samples"][1]["business_date"] = data["samples"][0]["business_date"]
    elif mutation == "future":
        data["samples"][-1]["business_date"] = "2026-10-22"
    elif mutation == "missing":
        data["samples"][-1]["members"].pop("00050")
    elif mutation == "nonfinite":
        data["samples"][-1]["program"] = float("nan")
    else:
        data["available_at"] = dt_ts(NOW) + 1
    expected = ccxt.ExchangeError if mutation == "nonfinite" else ValueError
    with pytest.raises(expected):
        train_model(data, NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"market": "UN"},
        {"unit": "KRW"},
        {"business_date": "2026-10-21"},
        {"members": {}},
        {"available_at": dt_ts(NOW.replace(hour=11))},
        {"collection_started_at": dt_ts(NOW - timedelta(seconds=1))},
    ],
)
def test_incompatible_or_future_inputs_fail(model, change):
    now = NOW.replace(hour=10)
    with pytest.raises(ValueError):
        predict_model(model, snapshot(model, now) | change, now)


def test_expired_and_stale_inputs_fail(model):
    now = NOW + timedelta(days=8)
    with pytest.raises(ValueError):
        predict_model(model, snapshot(model, now), now)
    now = NOW.replace(hour=10)
    inputs = snapshot(model, now)
    inputs["collection_started_at"] -= 300_000
    with pytest.raises(ValueError):
        predict_model(model, inputs, now)


def test_metrics_handle_constant_targets_without_nan():
    result = metrics([0, 0], [0, 0], [0, 0])
    assert result["r2"] is None and result["mae"] == 0
    assert np.isfinite(result["direction_agreement"])


@pytest.mark.parametrize(
    "field,value", [("scale", "1"), ("scale", True), ("coefficients", "1"), ("coefficients", False)]
)
def test_json_numeric_strings_and_booleans_are_rejected(model, field, value, tmp_path):
    altered = copy.deepcopy(model)
    if field == "scale":
        altered[field][0] = value
    else:
        altered[field][0][0] = value
    altered["model_id"] = fingerprint({k: v for k, v in altered.items() if k != "model_id"})
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match="artifact"):
        load_model(path)
