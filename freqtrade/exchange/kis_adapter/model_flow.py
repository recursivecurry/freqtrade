import argparse
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from filelock import FileLock

from freqtrade.configuration import Configuration
from freqtrade.enums import RunMode
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.flow_model import (
    load_model,
    predict_model,
    save_model,
    train_model,
)
from freqtrade.exchange.kis_adapter.flow_validation import evaluate_intraday, validation_allows
from freqtrade.exchange.kis_adapter.investor_flow import InvestorFlowStore
from freqtrade.exchange.kis_adapter.model_inputs import TARGETS, KisModelInputs
from freqtrade.exchange.kis_adapter.rules import SEOUL, Session
from freqtrade.util import dt_now, dt_ts


MODEL_LABEL = "장중 추정 수급 · 모델 추정치"


def collect_predictions(source, models, validations=None):
    records = []
    for model in models:
        record = {
            "symbol": model["symbol"],
            "kind": "model",
            "environment": "live",
            "market": "KRX",
            "unit": "shares",
            "label": MODEL_LABEL,
            "basis": "member_program_ridge_cumulative_quantity",
            "model_id": model["model_id"],
            "model_created_at": model["created_at"],
            "train_end_date": model["train_end_date"],
            "valid_until": model["valid_until"],
            "daily_metrics": model["daily_metrics"],
            "intraday_validation": "pending",
            "source_updated_at": None,
            "quantities": dict.fromkeys(TARGETS),
            "input_snapshot": None,
            "recipe_id": model["recipe_id"],
            "validation": None,
        }
        started = time.monotonic()
        try:
            now = dt_now()
            if not model["created_at"] <= dt_ts(now) < model["valid_until"]:
                raise ValueError("model outside availability window")
            snapshot = source.intraday(model["symbol"], model["members"])
            result = predict_model(model, snapshot, dt_now())
            record.update(quantities=result, input_snapshot=snapshot, availability="research_only")
            report = (validations or {}).get(model["symbol"])
            if validation_allows(report, model, now) and dt_ts(dt_now()) < report["valid_until"]:
                record.update(
                    availability="validated", intraday_validation="passed", validation=report
                )
        except Exception as exc:
            record["availability"] = "unavailable:" + type(exc).__name__
        now = dt_now()
        record.update(
            observed_at=dt_ts(now),
            inference_at=dt_ts(now),
            business_date=now.astimezone(SEOUL).date().isoformat(),
            collection_seconds=time.monotonic() - started,
        )
        records.append(record)
    return records


def enrich_model_flow(dataframe, pair, timeframe, path, publication_time, max_age_minutes=5):
    if type(max_age_minutes) is not int or not 1 <= max_age_minutes <= 5:
        raise ValueError("kis model max_age_minutes must be between one and five")
    result = dataframe.copy()
    selected = []
    store = InvestorFlowStore(path, read_only=True) if Path(path).exists() else None
    try:
        for stamp in result["date"]:
            decision = publication_time(timeframe, stamp.to_pydatetime())
            record = store.latest(pair, "model", dt_ts(decision)) if store and decision else None
            status = "unknown_candle_publication" if decision is None else "not_collected"
            if record:
                status = record["availability"]
                if (
                    record["business_date"] != decision.astimezone(SEOUL).date().isoformat()
                    or dt_ts(decision) - record["observed_at"] > max_age_minutes * 60_000
                    or dt_ts(decision) >= record["valid_until"]
                    or (
                        record.get("input_snapshot") is not None
                        and dt_ts(decision) - record["input_snapshot"]["collection_started_at"]
                        > max_age_minutes * 60_000
                    )
                ):
                    status = "stale_observation"
                if status == "validated" and (
                    record.get("validation") is None
                    or record["validation"]["available_at"]
                    > record["input_snapshot"]["collection_started_at"]
                    or dt_ts(decision) >= record["validation"]["valid_until"]
                ):
                    status = "validation_unavailable"
            selected.append((record, status))
    finally:
        if store:
            store.close()
    for who in TARGETS:
        result["kis_model_" + who + "_shares"] = pd.array(
            [
                record["quantities"][who] if record and status == "validated" else None
                for record, status in selected
            ],
            dtype="Float64",
        )
    result["kis_model_status"] = [status for _, status in selected]
    result["kis_model_label"] = MODEL_LABEL
    result["kis_model_unit"] = "shares"
    for key in (
        "model_id",
        "recipe_id",
        "business_date",
        "train_end_date",
        "basis",
        "intraday_validation",
    ):
        result["kis_model_" + key] = [record.get(key) if record else None for record, _ in selected]
    result["kis_model_observed_at"] = pd.to_datetime(
        [record["observed_at"] if record else None for record, _ in selected], unit="ms", utc=True
    )
    result["kis_model_inference_at"] = pd.to_datetime(
        [record.get("inference_at") if record else None for record, _ in selected],
        unit="ms",
        utc=True,
    )
    return result


def _parser():
    parser = argparse.ArgumentParser(description="Read-only KIS model-flow research tools")
    commands = parser.add_subparsers(dest="command", required=True)
    history = commands.add_parser("history")
    history.add_argument("--config", action="append", required=True)
    history.add_argument("--symbol", required=True)
    history.add_argument("--members", nargs="+", required=True)
    history.add_argument("--since", type=date.fromisoformat, required=True)
    history.add_argument("--until", type=date.fromisoformat, required=True)
    history.add_argument("--output", type=Path, required=True)
    train = commands.add_parser("train")
    train.add_argument("--config", action="append", required=True)
    train.add_argument("--history", type=Path, required=True)
    train.add_argument("--output", type=Path, required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--config", action="append", required=True)
    collect.add_argument("--model", type=Path, action="append", required=True)
    collect.add_argument("--database", type=Path, required=True)
    collect.add_argument("--interval", type=int, default=0)
    collect.add_argument("--validation-evidence", type=Path, action="append", default=[])
    validate = commands.add_parser("validate")
    validate.add_argument("--evidence", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--database", type=Path, required=True)
    inspect.add_argument("--symbol", required=True)
    inspect.add_argument("--asof", type=datetime.fromisoformat)
    return parser


def _configured(args):
    config = Configuration({"config": args.config}, RunMode.UTIL_EXCHANGE).get_config()
    exchange = config["exchange"]
    if exchange.get("name") != "kis" or exchange.get("kis", {}).get("environment") != "live":
        raise ValueError("production read-only KIS configuration required")
    return exchange


def _run_collection(args, exchange, source):
    models = [load_model(path) for path in args.model]
    symbols = [model["symbol"] for model in models]
    if len(set(symbols)) != len(symbols) or not set(symbols) <= set(exchange["pair_whitelist"]):
        raise ValueError("model symbols must be distinct and explicitly allowlisted")
    validations = {}
    for path in args.validation_evidence:
        report = evaluate_intraday(json.loads(path.read_text(encoding="utf-8")), dt_now())
        if report["symbol"] not in symbols or report["symbol"] in validations:
            raise ValueError("validation evidence must match one distinct model symbol")
        validations[report["symbol"]] = report
    with FileLock(str(args.database) + ".model.lock", timeout=0):
        store = InvestorFlowStore(args.database)
        try:
            while True:
                records = collect_predictions(source, models, validations)
                published_at = dt_ts(dt_now())
                for record in records:
                    record["observed_at"] = published_at
                inserted = store.save(records)
                failed = any(r["availability"].startswith("unavailable:") for r in records)
                print(
                    json.dumps(
                        {
                            "inserted": inserted,
                            "observations": len(records),
                            "request_failure": failed,
                            "broker_mutations": 0,
                            "collection_seconds": sum(r["collection_seconds"] for r in records),
                            "validated_observations": sum(
                                r["availability"] == "validated" for r in records
                            ),
                        }
                    ),
                    flush=True,
                )
                if not args.interval:
                    return 1 if failed else 0
                time.sleep(args.interval)
        finally:
            store.close()


def _train_command(args, exchange):
    now = dt_now()
    schedule = exchange["kis"].get("sessions", {})
    day = now.astimezone(SEOUL).date()
    if day.isoformat() not in schedule:
        raise ValueError("training requires a verified calendar")
    session = (
        Session.from_config(day, schedule[day.isoformat()]) if schedule[day.isoformat()] else None
    )
    if session and session.opening <= now < session.closing:
        raise ValueError("model training is disabled during the trading session")
    model = train_model(json.loads(args.history.read_text(encoding="utf-8")), now)
    save_model(model, args.output)
    print(
        json.dumps(
            {
                "model_id": model["model_id"],
                "daily_metrics": model["daily_metrics"],
                "intraday_validation": "pending",
            }
        )
    )
    return 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "collect" and args.interval != 0 and args.interval < 60:
        parser.error("interval must be zero (once) or at least 60 seconds")
    os.umask(0o077)
    prior_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    client = None
    try:
        if args.command == "validate":
            report = evaluate_intraday(
                json.loads(args.evidence.read_text(encoding="utf-8")), dt_now()
            )
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, allow_nan=False)
            print(json.dumps(report, allow_nan=False))
            return 0 if report["eligible"] else 1
        if args.command == "inspect":
            now = args.asof or dt_now()
            if now.tzinfo is None or now > dt_now():
                raise ValueError("inspection requires a nonfuture timezone-aware timestamp")
            store = InvestorFlowStore(args.database, read_only=True)
            try:
                print(json.dumps(store.latest(args.symbol, "model", dt_ts(now)), allow_nan=False))
            finally:
                store.close()
            return 0
        exchange = _configured(args)
        if args.command == "train":
            return _train_command(args, exchange)
        client = KisClient(
            {
                "apiKey": exchange.get("api_key"),
                "secret": exchange.get("secret"),
                "options": {"environment": "live"},
            }
        )
        source = KisModelInputs(client, exchange["kis"])
        if args.command == "history":
            if args.symbol not in exchange["pair_whitelist"]:
                raise ValueError("history symbol must be explicitly allowlisted")
            data = source.history(args.symbol, args.members, args.since, args.until)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, allow_nan=False)
            print(json.dumps({"complete_dates": len(data["samples"]), "broker_mutations": 0}))
            return 0
        return _run_collection(args, exchange, source)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("KIS model-flow command failed: " + type(exc).__name__)
        return 1
    finally:
        if client:
            client._token = None
            client._credentials = ("", "", "", "")
        logging.disable(prior_logging)


if __name__ == "__main__":
    raise SystemExit(main())
