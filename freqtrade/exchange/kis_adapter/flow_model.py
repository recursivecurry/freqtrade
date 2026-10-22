import hashlib
import json
import math
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from freqtrade.exchange.kis_adapter.model_inputs import TARGETS, quantity
from freqtrade.exchange.kis_adapter.rules import SEOUL
from freqtrade.util import dt_ts


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def metrics(actual, predicted, baseline):
    actual, predicted, baseline = (
        np.asarray(v, dtype=float) for v in (actual, predicted, baseline)
    )
    error = actual - predicted
    variance = float(np.sum((actual - actual.mean()) ** 2))
    return {
        "samples": len(actual),
        "mae": float(np.abs(error).mean()),
        "r2": 1.0 - float(error @ error) / variance if variance else None,
        "direction_agreement": float((np.sign(actual) == np.sign(predicted)).mean()),
        "zero_mae": float(np.abs(actual).mean()),
        "mean_mae": float(np.abs(actual - baseline).mean()),
    }


def passes(report):
    return (
        report["r2"] is not None
        and report["r2"] > 0
        and report["mae"] < min(report["zero_mae"], report["mean_mae"])
        and report["direction_agreement"] > 0.5
    )


def _fit(samples, dominant, alpha):
    candidates = sorted(samples[0]["members"])
    members = sorted(candidates, key=lambda m: (-sum(s["activity"][m] for s in samples), m))[
        :dominant
    ]
    x = np.array(
        [[s["members"][m] for m in members] + [s["program"]] for s in samples], dtype=float
    )
    y = np.array([[s["targets"][who] for who in TARGETS] for s in samples], dtype=float)
    scale = np.sqrt(np.mean(x * x, axis=0))
    scale[scale == 0] = 1.0
    normalized = x / scale
    coefficients = np.linalg.solve(
        normalized.T @ normalized + alpha * np.eye(x.shape[1]), normalized.T @ y
    )
    return members, scale, coefficients


def _training_samples(history, now, minimum):
    if (
        history.get("market") != "KRX"
        or history.get("unit") != "shares"
        or not re.fullmatch(r"[A-Z0-9]{6}/KRW", history.get("symbol", ""))
        or type(history.get("available_at")) is not int
        or history["available_at"] > dt_ts(now)
    ):
        raise ValueError("invalid KIS model history provenance")
    samples = sorted(history["samples"], key=lambda s: s["business_date"])
    if len(samples) < minimum:
        raise ValueError("insufficient complete dates for KIS walk-forward evaluation")
    if (now.astimezone(SEOUL).date() - date.fromisoformat(samples[-1]["business_date"])).days > 7:
        raise ValueError("KIS model training data is stale")
    candidates = set(samples[0]["members"])
    if not 1 <= len(candidates) <= 20 or any(not re.fullmatch(r"[0-9]{5}", m) for m in candidates):
        raise ValueError("invalid KIS model member candidates")
    seen = set()
    for sample in samples:
        day = date.fromisoformat(sample["business_date"])
        if day >= now.astimezone(SEOUL).date() or day in seen:
            raise ValueError("KIS model duplicate or nonhistorical training date")
        seen.add(day)
        if set(sample["members"]) != candidates or set(sample["activity"]) != candidates:
            raise ValueError("KIS model incomplete member training inputs")
        for value in [*sample["members"].values(), sample["program"], *sample["targets"].values()]:
            quantity(value)
        if set(sample["targets"]) != set(TARGETS):
            raise ValueError("KIS model training target mismatch")
        if any(quantity(v) < 0 for v in sample["activity"].values()):
            raise ValueError("KIS model negative activity")
    return samples


def train_model(
    history, now, *, training_days=20, evaluation_days=10, dominant_members=10, alpha=1.0
):
    if (
        now.tzinfo is None
        or type(training_days) is not int
        or training_days < 20
        or type(evaluation_days) is not int
        or evaluation_days < 10
        or type(dominant_members) is not int
        or not 1 <= dominant_members <= 20
        or isinstance(alpha, bool)
        or not isinstance(alpha, (int, float))
        or not math.isfinite(alpha)
        or alpha <= 0
    ):
        raise ValueError("invalid KIS model training parameters")
    samples = _training_samples(history, now, training_days + evaluation_days)
    actual, predicted, baseline, folds = [], [], [], []
    for index in range(len(samples) - evaluation_days, len(samples)):
        preceding = samples[index - training_days : index]
        members, scale, coefficients = _fit(preceding, dominant_members, alpha)
        row = samples[index]
        x = np.array([row["members"][m] for m in members] + [row["program"]], dtype=float)
        actual.append([row["targets"][who] for who in TARGETS])
        predicted.append((x / scale) @ coefficients)
        baseline.append([np.mean([s["targets"][who] for s in preceding]) for who in TARGETS])
        folds.append(
            {
                "business_date": row["business_date"],
                "train_end_date": preceding[-1]["business_date"],
                "members": members,
            }
        )
    a, p, b = map(np.asarray, (actual, predicted, baseline))
    report = {who: metrics(a[:, i], p[:, i], b[:, i]) for i, who in enumerate(TARGETS)}
    report["foreign_minus_individual"] = metrics(
        a[:, 0] - a[:, 2], p[:, 0] - p[:, 2], b[:, 0] - b[:, 2]
    )
    members, scale, coefficients = _fit(samples[-training_days:], dominant_members, alpha)
    model = {
        "schema": 2,
        "symbol": history["symbol"],
        "market": "KRX",
        "unit": "shares",
        "targets": list(TARGETS),
        "members": members,
        "scale": scale.tolist(),
        "coefficients": coefficients.tolist(),
        "fit_intercept": False,
        "alpha": alpha,
        "training_days": training_days,
        "evaluation_days": evaluation_days,
        "train_end_date": samples[-1]["business_date"],
        "created_at": dt_ts(now),
        "history_available_at": history["available_at"],
        "history_sha256": fingerprint(history),
        "valid_until": dt_ts(now + timedelta(days=7)),
        "daily_metrics": report,
        "folds": folds,
        "daily_gate_passed": all(passes(value) for value in report.values()),
        "intraday_validation": "pending",
        "training_target_mean": {
            who: float(np.mean([s["targets"][who] for s in samples[-training_days:]]))
            for who in TARGETS
        },
        "recipe": {
            "algorithm": "ridge_rms_no_intercept_v1",
            "symbol": history["symbol"],
            "training_days": training_days,
            "evaluation_days": evaluation_days,
            "dominant_members": min(dominant_members, len(samples[0]["members"])),
            "alpha": alpha,
            "candidates": sorted(samples[0]["members"]),
        },
    }
    model["recipe_id"] = fingerprint(model["recipe"])
    model["model_id"] = fingerprint(model)
    validate_model(model)
    return model


def validate_model(model):
    try:
        core = {k: v for k, v in model.items() if k != "model_id"}
        if model["model_id"] != fingerprint(core):
            raise ValueError
        members = model["members"]
        dimensions = len(members) + 1
        scalars = [*model["scale"], *(v for row in model["coefficients"] for v in row)]
        scalars += list(model["training_target_mean"].values())
        if any(type(value) not in (int, float) for value in scalars):
            raise ValueError
        scale, coefficients = (
            np.asarray(model["scale"], dtype=float),
            np.asarray(model["coefficients"], dtype=float),
        )
        if (
            model["schema"] != 2
            or model["market"] != "KRX"
            or model["unit"] != "shares"
            or model["targets"] != list(TARGETS)
            or model["fit_intercept"] is not False
            or not re.fullmatch(r"[A-Z0-9]{6}/KRW", model["symbol"])
            or not 1 <= len(members) <= 20
            or len(set(members)) != len(members)
            or any(not re.fullmatch(r"[0-9]{5}", m) for m in members)
            or scale.shape != (dimensions,)
            or coefficients.shape != (dimensions, 3)
            or not np.isfinite(scale).all()
            or not (scale > 0).all()
            or not np.isfinite(coefficients).all()
            or any(
                type(model[k]) is not int
                for k in ("created_at", "history_available_at", "valid_until")
            )
            or not model["history_available_at"] <= model["created_at"] < model["valid_until"]
            or model["valid_until"] - model["created_at"] > 7 * 86_400_000
            or date.fromisoformat(model["train_end_date"])
            >= datetime.fromtimestamp(model["created_at"] / 1000, SEOUL).date()
            or model["intraday_validation"] != "pending"
            or set(model["training_target_mean"]) != set(TARGETS)
            or set(model["daily_metrics"]) != {*TARGETS, "foreign_minus_individual"}
            or type(model["training_days"]) is not int
            or model["training_days"] < 20
            or type(model["evaluation_days"]) is not int
            or model["evaluation_days"] < 10
            or type(model["alpha"]) not in (int, float)
            or model["alpha"] <= 0
        ):
            raise ValueError
        recipe = model["recipe"]
        candidates = recipe["candidates"]
        if (
            model["recipe_id"] != fingerprint(recipe)
            or recipe["algorithm"] != "ridge_rms_no_intercept_v1"
            or recipe["symbol"] != model["symbol"]
            or recipe["training_days"] != model["training_days"]
            or recipe["evaluation_days"] != model["evaluation_days"]
            or recipe["alpha"] != model["alpha"]
            or recipe["dominant_members"] != len(members)
            or not 1 <= len(candidates) <= 20
            or len(set(candidates)) != len(candidates)
            or any(not re.fullmatch(r"[0-9]{5}", m) for m in candidates)
            or not set(members) <= set(candidates)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("invalid KIS flow model artifact") from None
    return model


def save_model(model, path):
    validate_model(model)
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(model, stream, allow_nan=False, sort_keys=True)


def load_model(path):
    return validate_model(json.loads(Path(path).read_text(encoding="utf-8")))


def predict_model(model, snapshot, now):
    validate_model(model)
    when = dt_ts(now)
    if (
        now.tzinfo is None
        or snapshot.get("symbol") != model["symbol"]
        or snapshot.get("market") != "KRX"
        or snapshot.get("unit") != "shares"
        or snapshot.get("business_date") != now.astimezone(SEOUL).date().isoformat()
        or type(snapshot.get("available_at")) is not int
        or type(snapshot.get("collection_started_at")) is not int
        or not model["created_at"]
        <= snapshot["collection_started_at"]
        <= snapshot["available_at"]
        <= when
        < model["valid_until"]
        or when - snapshot["collection_started_at"] > 300_000
        or date.fromisoformat(model["train_end_date"]) >= now.astimezone(SEOUL).date()
        or (now.astimezone(SEOUL).date() - date.fromisoformat(model["train_end_date"])).days > 7
        or set(snapshot["members"]) != set(model["members"])
    ):
        raise ValueError("KIS model or inputs unavailable at decision time")
    x = np.array(
        [quantity(snapshot["members"][m]) for m in model["members"]]
        + [quantity(snapshot["program"])],
        dtype=float,
    )
    prediction = (x / model["scale"]) @ np.array(model["coefficients"], dtype=float)
    if not np.isfinite(prediction).all() or (np.abs(prediction) > 2**53 - 1).any():
        raise ValueError("KIS model prediction outside numeric precision")
    return dict(zip(TARGETS, map(float, prediction), strict=True))
