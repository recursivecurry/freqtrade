import hashlib
import json
import re
from datetime import date

import ccxt

from freqtrade.exchange.kis_adapter.market_data import KisMarketData
from freqtrade.exchange.kis_adapter.rules import SEOUL, number
from freqtrade.util import dt_now, dt_ts


TARGETS = {
    "foreign": "frgn_ntby_qty",
    "institution": "orgn_ntby_qty",
    "individual": "prsn_ntby_qty",
}
INPUT_SOURCES = {"member": "FHPST04540000", "program": "FHPPG04650201"}


def quantity(value):
    result = number(value, integer=True, signed=True)
    if abs(result) > 2**53 - 1:
        raise ccxt.ExchangeError("kis model quantity exceeds exact numeric precision")
    return int(result)


def dated_rows(payload, key, fields, today):
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise ccxt.ExchangeError("kis model response has no dated rows")
    result = {}
    for row in rows:
        try:
            raw = row["stck_bsop_date"]
            if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{8}", raw):
                raise ValueError
            day = date.fromisoformat(raw[:4] + "-" + raw[4:6] + "-" + raw[6:])
            if day > today:
                raise ValueError
            values = {name: quantity(row.get(field)) for name, field in fields.items()}
        except (KeyError, TypeError, ValueError):
            raise ccxt.ExchangeError("kis model invalid business date or row") from None
        if day.isoformat() in result and result[day.isoformat()] != values:
            raise ccxt.ExchangeError("kis model conflicting dated rows")
        result[day.isoformat()] = values
    return result


class KisModelInputs:
    def __init__(self, client, options):
        if client.environment != "live" or client.paper_orders:
            raise ccxt.NotSupported("kis model inputs require a production read-only client")
        self.client = client
        self.data = KisMarketData(client, options)

    def _read(self, endpoint, tr_id, params, key, fields):
        started = dt_now()
        payload, continuation = self.client.get("quotations/" + endpoint, tr_id, params)
        received = dt_now()
        if continuation not in ("", "D", "E") or received < started:
            raise ccxt.ExchangeError("kis model incomplete response or clock rollback")
        rows = dated_rows(payload, key, fields, received.astimezone(SEOUL).date())
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return rows, {
            "endpoint": endpoint,
            "tr_id": tr_id,
            "request_started_at": dt_ts(started),
            "received_at": dt_ts(received),
            "source_updated_at": None,
            "response_sha256": digest,
        }

    def _inputs(self, symbol, members, first, last):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}/KRW", symbol):
            raise ccxt.BadSymbol("kis model requires an explicit domestic KRW symbol")
        if (
            not isinstance(members, list)
            or not 1 <= len(members) <= 20
            or any(not isinstance(m, str) or not re.fullmatch(r"[0-9]{5}", m) for m in members)
            or len(set(members)) != len(members)
        ):
            raise ccxt.BadRequest("kis model requires 1 to 20 distinct member codes")
        if not isinstance(first, date) or not isinstance(last, date) or first > last:
            raise ccxt.BadRequest("kis model invalid input date range")
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol.removesuffix("/KRW")}
        sources, features, activity = [], {}, {}
        for member in members:
            rows, receipt = self._read(
                "inquire-member-daily",
                INPUT_SOURCES["member"],
                params
                | {
                    "FID_INPUT_ISCD_2": member,
                    "FID_INPUT_DATE_1": first.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": last.strftime("%Y%m%d"),
                    "FID_SCTN_CLS_CODE": "",
                },
                "output",
                {"net": "ntby_qty", "buy": "total_shnu_qty", "sell": "total_seln_qty"},
            )
            sources.append(receipt | {"member_code": member})
            for day, values in rows.items():
                if first.isoformat() <= day <= last.isoformat():
                    if (
                        values["buy"] < 0
                        or values["sell"] < 0
                        or values["net"] != values["buy"] - values["sell"]
                    ):
                        raise ccxt.ExchangeError("kis model inconsistent member quantity")
                    features.setdefault(day, {})[member] = values["net"]
                    activity.setdefault(day, {})[member] = values["buy"] + values["sell"]
        programs, receipt = self._read(
            "program-trade-by-stock-daily",
            INPUT_SOURCES["program"],
            params | {"FID_INPUT_DATE_1": last.strftime("%Y%m%d")},
            "output",
            {"program": "whol_smtn_ntby_qty"},
        )
        sources.append(receipt)
        samples = []
        for day in sorted(features):
            if set(features[day]) == set(members) and day in programs:
                samples.append(
                    {
                        "business_date": day,
                        "members": features[day],
                        "activity": activity[day],
                        "program": programs[day]["program"],
                    }
                )
        return params, samples, sources

    def history(self, symbol, members, first, last):
        if type(first) is not date or type(last) is not date or first > last:
            raise ccxt.BadRequest("kis model invalid history date range")
        if last >= dt_now().astimezone(SEOUL).date():
            raise ccxt.BadRequest("kis model training history must end before today")
        params, samples, sources = self._inputs(symbol, members, first, last)
        targets, receipt = self._read(
            "investor-trade-by-stock-daily",
            "FHPTJ04160001",
            params
            | {
                "FID_INPUT_DATE_1": last.strftime("%Y%m%d"),
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            },
            "output2",
            TARGETS,
        )
        sources.append(receipt)
        samples = [
            sample | {"targets": targets[sample["business_date"]]}
            for sample in samples
            if sample["business_date"] in targets
        ]
        return {
            "symbol": symbol,
            "market": "KRX",
            "unit": "shares",
            "samples": samples,
            "sources": sources,
            "available_at": dt_ts(dt_now()),
        }

    def intraday(self, symbol, members, max_span_seconds=120):
        if type(max_span_seconds) is not int or not 1 <= max_span_seconds <= 300:
            raise ccxt.BadRequest("kis model invalid collection span")
        started = dt_now()
        day = started.astimezone(SEOUL).date()
        session = self.data.session(day)
        if not session or not session.opening <= started < session.closing:
            raise ccxt.ExchangeError("kis model requires a verified active session")
        _, samples, sources = self._inputs(symbol, members, day, day)
        received = dt_now()
        if (
            received < started
            or received >= session.closing
            or (received - started).total_seconds() > max_span_seconds
        ):
            raise ccxt.ExchangeError("kis model collection crossed its time window")
        if len(samples) != 1 or samples[0]["business_date"] != day.isoformat():
            raise ccxt.ExchangeError("kis model incomplete current-day inputs")
        return {
            "symbol": symbol,
            "market": "KRX",
            "unit": "shares",
            **samples[0],
            "sources": sources,
            "collection_started_at": dt_ts(started),
            "available_at": dt_ts(received),
            "source_updated_at": None,
            "freshness": "source_update_unknown",
        }
