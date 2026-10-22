import argparse
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import ccxt
import pandas as pd

from freqtrade.configuration import Configuration
from freqtrade.enums import RunMode
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.market_data import KisMarketData
from freqtrade.exchange.kis_adapter.rules import SEOUL, number
from freqtrade.util import dt_now, dt_ts


PARTICIPANTS = {"foreign": "frgn", "institution": "orgn", "individual": "prsn"}
ESTIMATE_SOURCE = "FHPTJ04400000"
DAILY_SOURCE = "FHPTJ04160001"


def amounts(row, estimate=False):
    values, reasons = {}, {}
    for name, prefix in PARTICIPANTS.items():
        raw = row.get(prefix + "_ntby_tr_pbmn")
        if estimate and name == "individual":
            values[name], reasons[name] = None, "individual_intraday_unavailable"
        elif raw is None or raw == "":
            values[name], reasons[name] = None, "not_reported"
        else:
            value = number(raw, signed=True) * 1_000_000
            if value != value.to_integral_value() or abs(value) > 2**63 - 1:
                raise ccxt.ExchangeError("kis investor amount exceeds whole-KRW storage precision")
            values[name], reasons[name] = int(value), None
    return values, reasons


def observation(symbol, kind, when, *, row=None, business_date=None, reason=None):
    estimate = kind == "estimate"
    values, reasons = amounts(row or {}, estimate) if not reason else ({}, {})
    if reason:
        values = dict.fromkeys(PARTICIPANTS)
        reasons = dict.fromkeys(PARTICIPANTS, reason)
    return {
        "symbol": symbol,
        "environment": "live",
        "kind": kind,
        "observed_at": dt_ts(when),
        "observed_date": when.astimezone(SEOUL).date().isoformat(),
        "business_date": business_date,
        "source_updated_at": None,
        "source": ESTIMATE_SOURCE if estimate else DAILY_SOURCE,
        "source_unit": "million_KRW",
        "currency": "KRW",
        "estimated": estimate,
        "basis": "broker_quantity_times_current_price"
        if estimate
        else "broker_reported_net_amount",
        "availability": reason or ("business_date_unknown" if estimate else "reported_revisable"),
        "amounts": values,
        "reasons": reasons,
    }


class KisInvestorFlow:
    def __init__(self, client, options):
        if client.environment != "live" or client.paper_orders:
            raise ccxt.NotSupported(
                "kis investor collection requires a production read-only client"
            )
        self.client = client
        self.data = KisMarketData(client, options)

    def _ranked(self):
        ranked, conflicts = {}, set()
        for direction in ("0", "1"):
            payload, continuation = self.client.get(
                "quotations/foreign-institution-total",
                ESTIMATE_SOURCE,
                {
                    "FID_COND_MRKT_DIV_CODE": "V",
                    "FID_COND_SCR_DIV_CODE": "16449",
                    "FID_INPUT_ISCD": "0000",
                    "FID_DIV_CLS_CODE": "1",
                    "FID_RANK_SORT_CLS_CODE": direction,
                    "FID_ETC_CLS_CODE": "0",
                },
            )
            if continuation not in ("", "D", "E") or not isinstance(payload.get("output"), list):
                raise ccxt.ExchangeError("kis investor ranking response is incomplete")
            for row in payload["output"]:
                if not isinstance(row, dict):
                    raise ccxt.ExchangeError("kis invalid investor ranking row")
                code = row.get("mksc_shrn_iscd", "")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{6}", code):
                    raise ccxt.ExchangeError("kis invalid investor ranking symbol")
                values = amounts(row, True)
                if code in ranked and amounts(ranked[code], True) != values:
                    conflicts.add(code)
                ranked[code] = row
        return ranked, conflicts

    def _published(self, day, when):
        today = when.astimezone(SEOUL).date()
        if day > today:
            raise ccxt.ExchangeError("kis investor date is in the future")
        if day < today:
            return True
        try:
            session = self.data.session(today)
        except ccxt.BaseError:
            return False
        return bool(session and when >= session.closing + timedelta(minutes=10))

    def _daily(self, symbol):
        payload, continuation = self.client.get(
            "quotations/investor-trade-by-stock-daily",
            DAILY_SOURCE,
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol.removesuffix("/KRW"),
                "FID_INPUT_DATE_1": dt_now().astimezone(SEOUL).strftime("%Y%m%d"),
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            },
        )
        when = dt_now()
        if continuation not in ("", "D", "E") or not isinstance(payload.get("output2"), list):
            raise ccxt.ExchangeError("kis dated investor response is incomplete")
        dated = {}
        for row in payload["output2"]:
            if not isinstance(row, dict):
                raise ccxt.ExchangeError("kis invalid dated investor row")
            try:
                text = row["stck_bsop_date"]
                if not isinstance(text, str) or not re.fullmatch(r"[0-9]{8}", text):
                    raise ValueError
                day = datetime.strptime(text, "%Y%m%d").date()
            except (KeyError, ValueError, TypeError):
                raise ccxt.ExchangeError("kis invalid investor business date") from None
            if not self._published(day, when):
                continue
            values, reasons = amounts(row)
            item = {"business_date": day.isoformat(), "amounts": values, "reasons": reasons}
            if day in dated and dated[day] != item:
                raise ccxt.ExchangeError("kis conflicting dated investor rows")
            dated[day] = item
        history = [dated[day] for day in sorted(dated)]
        latest = history[-1] if history else None
        snapshot = observation(
            symbol,
            "daily",
            when,
            business_date=latest["business_date"] if latest else None,
            reason=None if latest else "no_published_daily_rows",
        )
        if latest:
            snapshot.update(amounts=latest["amounts"], reasons=latest["reasons"])
        snapshot["history"] = history
        return snapshot

    def collect(self, symbols):
        symbols = list(dict.fromkeys(symbols))
        if not symbols or any(
            not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{6}/KRW", s) for s in symbols
        ):
            raise ccxt.BadSymbol("kis investor collection requires explicit domestic KRW symbols")
        ranked, conflicts, failure = {}, set(), None
        try:
            ranked, conflicts = self._ranked()
        except ccxt.BaseError as exc:
            failure = "request_failed:" + type(exc).__name__
        estimate_when = dt_now()
        result = []
        for symbol in symbols:
            code = symbol.removesuffix("/KRW")
            reason = failure or (
                "conflicting_rank_rows"
                if code in conflicts
                else "not_in_ranked_response"
                if code not in ranked
                else None
            )
            result.append(
                observation(symbol, "estimate", estimate_when, row=ranked.get(code), reason=reason)
            )
            try:
                result.append(self._daily(symbol))
            except ccxt.BaseError as exc:
                result.append(
                    observation(
                        symbol, "daily", dt_now(), reason="request_failed:" + type(exc).__name__
                    )
                )
        return result


class InvestorFlowStore:
    def __init__(self, path, *, read_only=False):
        path = Path(path).resolve()
        self.db = (
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            if read_only
            else sqlite3.connect(path, timeout=5)
        )
        self.db.row_factory = sqlite3.Row
        if not read_only:
            path.chmod(0o600)
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS kis_investor_observations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, kind TEXT NOT NULL,
                    observed_at INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS kis_investor_lookup
                    ON kis_investor_observations(symbol, kind, observed_at);
                CREATE TABLE IF NOT EXISTS kis_investor_receipts (
                    symbol TEXT NOT NULL, kind TEXT NOT NULL,
                    received_at INTEGER NOT NULL, PRIMARY KEY(symbol, kind)
                );
                INSERT OR IGNORE INTO kis_investor_receipts(symbol,kind,received_at)
                    SELECT symbol,kind,max(observed_at) FROM kis_investor_observations
                    GROUP BY symbol,kind;
            """)

    def close(self):
        self.db.close()

    def latest(self, symbol, kind, asof):
        row = self.db.execute(
            "SELECT payload FROM kis_investor_observations WHERE symbol=? AND kind=? "
            "AND observed_at<=? ORDER BY observed_at DESC, sequence DESC LIMIT 1",
            (symbol, kind, asof),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, records):
        inserted = 0
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for record in records:
                previous = self.latest(record["symbol"], record["kind"], 2**63 - 1)
                receipt = self.db.execute(
                    "SELECT received_at FROM kis_investor_receipts WHERE symbol=? AND kind=?",
                    (record["symbol"], record["kind"]),
                ).fetchone()
                if receipt and record["observed_at"] < receipt[0]:
                    raise ccxt.ExchangeError("kis investor observation clock moved backwards")
                self.db.execute(
                    "INSERT INTO kis_investor_receipts(symbol,kind,received_at) VALUES(?,?,?) "
                    "ON CONFLICT(symbol,kind) DO UPDATE SET received_at=excluded.received_at",
                    (record["symbol"], record["kind"], record["observed_at"]),
                )
                stable = {k: v for k, v in record.items() if k != "observed_at"}
                if previous:
                    if stable == {k: v for k, v in previous.items() if k != "observed_at"}:
                        continue
                    if receipt and record["observed_at"] == receipt[0]:
                        raise ccxt.ExchangeError(
                            "kis conflicting investor observations at one time"
                        )
                self.db.execute(
                    "INSERT INTO kis_investor_observations(symbol,kind,observed_at,payload) "
                    "VALUES(?,?,?,?)",
                    (record["symbol"], record["kind"], record["observed_at"], json.dumps(record)),
                )
                inserted += 1
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return inserted


def enrich_investor_flow(dataframe, pair, timeframe, path, publication_time, max_age_minutes=180):
    if type(max_age_minutes) is not int or max_age_minutes < 1:
        raise ValueError("kis investor max_age_minutes must be positive")
    result = dataframe.copy()
    columns = {}
    store = InvestorFlowStore(path, read_only=True) if Path(path).exists() else None
    try:
        for kind in ("estimate", "daily"):
            selected = []
            for stamp in result["date"]:
                decision = publication_time(timeframe, stamp.to_pydatetime())
                record = store.latest(pair, kind, dt_ts(decision)) if store and decision else None
                reason = "unknown_candle_publication" if decision is None else "not_collected"
                if record:
                    reason = record["availability"]
                    if kind == "estimate" and (
                        record["observed_date"] != decision.astimezone(SEOUL).date().isoformat()
                        or dt_ts(decision) - record["observed_at"] > max_age_minutes * 60_000
                    ):
                        reason = "stale_observation"
                    values = (
                        record["amounts"]
                        if reason != "stale_observation"
                        else dict.fromkeys(PARTICIPANTS)
                    )
                else:
                    values = dict.fromkeys(PARTICIPANTS)
                selected.append((record, reason, values))
            prefix = "kis_" + kind + "_"
            for who in PARTICIPANTS:
                columns[prefix + who + "_krw"] = pd.array(
                    [values[who] for _, _, values in selected], dtype="Int64"
                )
                columns[prefix + who + "_reason"] = [
                    record["reasons"][who] if record and reason != "stale_observation" else reason
                    for record, reason, _ in selected
                ]
            columns[prefix + "status"] = [reason for _, reason, _ in selected]
            columns[prefix + "business_date"] = [
                record["business_date"] if record else None for record, _, _ in selected
            ]
            columns[prefix + "observed_at"] = pd.to_datetime(
                [record["observed_at"] if record else None for record, _, _ in selected],
                unit="ms",
                utc=True,
            )
            columns[prefix + "basis"] = [
                record["basis"] if record else None for record, _, _ in selected
            ]
        for name, values in columns.items():
            result[name] = values
    finally:
        if store:
            store.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect read-only KIS investor observations")
    parser.add_argument("--config", action="append", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=0)
    args = parser.parse_args(argv)
    if args.interval != 0 and args.interval < 60:
        parser.error("interval must be zero (once) or at least 60 seconds")
    os.umask(0o077)
    prior_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    store = None
    client = None
    try:
        config = Configuration({"config": args.config}, RunMode.UTIL_EXCHANGE).get_config()
        exchange = config["exchange"]
        if exchange.get("name") != "kis" or exchange.get("kis", {}).get("environment") != "live":
            raise ValueError("production read-only KIS configuration required")
        client = KisClient(
            {
                "apiKey": exchange.get("api_key"),
                "secret": exchange.get("secret"),
                "options": {"environment": "live"},
            }
        )
        source = KisInvestorFlow(client, exchange["kis"])
        store = InvestorFlowStore(args.database)
        while True:
            records = source.collect(exchange["pair_whitelist"])
            count = store.save(records)
            failed = any(r["availability"].startswith("request_failed:") for r in records)
            print(
                json.dumps(
                    {
                        "inserted": count,
                        "observations": len(records),
                        "request_failure": failed,
                        "broker_mutations": 0,
                    }
                ),
                flush=True,
            )
            if not args.interval:
                return 1 if failed else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("KIS investor collection failed: " + type(exc).__name__)
        return 1
    finally:
        if store:
            store.close()
        if client:
            client._token = None
            client._credentials = ("", "", "", "")
        logging.disable(prior_logging)


if __name__ == "__main__":
    raise SystemExit(main())
