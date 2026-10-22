import re
from datetime import date, datetime, timedelta

import numpy as np

from freqtrade.exchange.kis_adapter.flow_model import (
    fingerprint,
    metrics,
    passes,
    predict_model,
    validate_model,
)
from freqtrade.exchange.kis_adapter.model_inputs import TARGETS, quantity
from freqtrade.exchange.kis_adapter.rules import SEOUL, Session
from freqtrade.util import dt_ts


PROTOCOL = "kis-flow-intraday-v1"
WINDOWS = ("opening", "midday", "afternoon")


def window_for(when):
    hhmm = when.astimezone(SEOUL).strftime("%H%M")
    return "opening" if hhmm < "1000" else "midday" if hhmm < "1400" else "afternoon"


def _aligned_pair(pair, models, evidence, now):
    record, reference = pair["prediction"], pair["reference"]
    model = models[record["model_id"]]
    observed = record["observed_at"]
    when = datetime.fromtimestamp(record["inference_at"] / 1000, SEOUL)
    day = when.date().isoformat()
    session = Session.from_config(when.date(), evidence["sessions"][day])
    snapshot = record["input_snapshot"]
    if (
        record["symbol"] != evidence["symbol"]
        or model["recipe_id"] != evidence["recipe_id"]
        or record["kind"] != "model"
        or record["unit"] != "shares"
        or record["market"] != "KRX"
        or reference["symbol"] != record["symbol"]
        or reference["market"] != "KRX"
        or reference["unit"] != "shares"
        or reference["business_date"] != day
        or record["business_date"] != day
        or reference["frequency"] != "intraday"
        or reference["basis"] != "reported_cumulative_net_quantity"
        or type(reference["asof"]) is not int
        or reference["asof"] != snapshot["available_at"]
        or type(reference["available_at"]) is not int
        or not snapshot["available_at"] <= reference["available_at"] <= dt_ts(now)
        or type(evidence["protocol_registered_at"]) is not int
        or not model["created_at"] <= observed <= dt_ts(now)
        or not model["created_at"] <= record["inference_at"] <= observed
        or not evidence["protocol_registered_at"] < snapshot["collection_started_at"]
        or reference["cumulative_from"] != dt_ts(session.opening)
        or snapshot["collection_started_at"] < dt_ts(session.opening)
        or not session.opening <= when < session.closing
        or not isinstance(reference["source"], str)
        or not reference["source"].strip()
        or not isinstance(reference["response_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", reference["response_sha256"])
        or set(reference["targets"]) != set(TARGETS)
    ):
        raise ValueError("KIS validation pair has incompatible provenance or times")
    expected = predict_model(model, snapshot, when)
    if record["quantities"] != expected:
        raise ValueError("KIS validation prediction cannot be reproduced")
    actual = [quantity(reference["targets"][who]) for who in TARGETS]
    predicted = [expected[who] for who in TARGETS]
    fraction = (when - session.opening) / (session.closing - session.opening)
    baseline = [model["training_target_mean"][who] * fraction for who in TARGETS]
    return day, window_for(when), snapshot["available_at"], actual, predicted, baseline


def evaluate_intraday(evidence, now):
    if now.tzinfo is None or evidence.get("protocol") != PROTOCOL:
        raise ValueError("KIS validation requires the registered intraday protocol")
    models = {}
    for model in evidence["models"]:
        validate_model(model)
        if model["model_id"] in models:
            raise ValueError("KIS validation duplicate model")
        models[model["model_id"]] = model
    groups = {window: [] for window in WINDOWS}
    seen, dates = set(), set()
    for pair in evidence["pairs"]:
        day, window, observed, actual, predicted, baseline = _aligned_pair(
            pair, models, evidence, now
        )
        if observed in seen:
            raise ValueError("KIS validation duplicate observation")
        seen.add(observed)
        dates.add(day)
        groups[window].append((day, actual, predicted, baseline))
    reports = {}
    for window, rows in groups.items():
        report = {"pairs": len(rows), "dates": len({row[0] for row in rows}), "metrics": {}}
        if rows:
            a, p, b = (np.array([row[i] for row in rows], dtype=float) for i in (1, 2, 3))
            report["metrics"] = {
                who: metrics(a[:, i], p[:, i], b[:, i]) for i, who in enumerate(TARGETS)
            }
            report["metrics"]["foreign_minus_individual"] = metrics(
                a[:, 0] - a[:, 2], p[:, 0] - p[:, 2], b[:, 0] - b[:, 2]
            )
        report["passed"] = (
            report["dates"] >= 10
            and report["pairs"] >= 30
            and all(passes(value) for value in report["metrics"].values())
        )
        reports[window] = report
    latest = date.fromisoformat(max(dates)) if dates else None
    recent = (
        latest is not None
        and (now.astimezone(SEOUL).date() - latest).days <= 14
        and (now.astimezone(SEOUL).date() - date.fromisoformat(min(dates))).days <= 60
    )
    summary = {
        "protocol": PROTOCOL,
        "symbol": evidence["symbol"],
        "recipe_id": evidence["recipe_id"],
        "evidence_sha256": fingerprint(evidence),
        "available_at": dt_ts(now),
        "dates": len(dates),
        "windows": reports,
        "eligible": bool(recent and all(r["passed"] for r in reports.values())),
        "valid_until": dt_ts(
            datetime.combine(latest + timedelta(days=15), datetime.min.time(), SEOUL)
        )
        if latest
        else dt_ts(now),
        "alignment": "receipt_time_with_unknown_source_latency",
    }
    summary["report_id"] = fingerprint(summary)
    return summary


def validation_allows(report, model, now):
    if report is None:
        return False
    expected = fingerprint({k: v for k, v in report.items() if k != "report_id"})
    return (
        report["report_id"] == expected
        and report["protocol"] == PROTOCOL
        and report["symbol"] == model["symbol"]
        and report["recipe_id"] == model["recipe_id"]
        and report["eligible"] is True
        and report["available_at"] <= dt_ts(now) < report["valid_until"]
        and all(passes(value) for value in model["daily_metrics"].values())
    )
