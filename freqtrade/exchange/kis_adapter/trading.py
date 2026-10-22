"""Paper-account reconciliation and mutation policy over the serialized KIS client."""

import copy
import hashlib
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import ccxt
from dateutil.relativedelta import relativedelta

from freqtrade.exchange.kis_adapter.costs import estimate_cost
from freqtrade.exchange.kis_adapter.market_data import KisMarketData
from freqtrade.exchange.kis_adapter.orders import order_id, parse_balance_page, parse_order
from freqtrade.exchange.kis_adapter.rules import SEOUL, broker_timestamp, number
from freqtrade.util import dt_from_ts, dt_now, dt_ts


if TYPE_CHECKING:
    from freqtrade.exchange.kis_adapter.store import KisStore


class KisTrading:
    CAPABILITIES = (
        "fetchBalance",
        "fetchOrders",
        "fetchOpenOrders",
        "fetchOrder",
        "fetchMyTrades",
        "createOrder",
        "cancelOrder",
    )

    @classmethod
    def bind(cls, client, options):
        if hasattr(client, "_trading"):
            if client._trading.options != options:
                raise ccxt.AuthenticationError("kis shared trading options mismatch")
        else:
            client._trading = cls(client, options)
        return client._trading

    @staticmethod
    def validate_filters(since, limit):
        if (since is not None and (type(since) is not int or since < 0)) or (
            limit is not None and (type(limit) is not int or not 1 <= limit <= 10000)
        ):
            raise ccxt.BadRequest("kis requires nonnegative integer since and limit in 1..10000")

    def __init__(self, client, options):
        self.client = client
        self.options = copy.deepcopy(options)
        self.data = KisMarketData(client, options)
        self.scope = hashlib.sha256(
            (client.environment + ":" + ":".join(client._credentials)).encode()
        ).hexdigest()[:24]
        self._store: KisStore | None = None

    @property
    def store(self):
        if self.client._closed:
            raise ccxt.ExchangeNotAvailable("kis account client is closed")
        if not self.client.paper_orders:
            raise ccxt.NotSupported("kis private trading currently requires explicit paper mode")
        if self._store is None:
            from freqtrade.exchange.kis_adapter.store import KisStore

            self._store = KisStore(self.options.get("dbUrl", ""), self.scope)
        return self._store

    def close(self):
        if self._store:
            self._store.close()

    @contextmanager
    def _operation(self):
        with self.client._lock:
            if self.client._closed:
                raise ccxt.ExchangeNotAvailable("kis account client is closed")
            try:
                yield
            except sqlite3.Error:
                raise ccxt.ExchangeError(
                    "kis durable state is unavailable; reconcile before trading"
                ) from None

    def _account(self):
        return {"CANO": self.client._credentials[2], "ACNT_PRDT_CD": self.client._credentials[3]}

    def _fee(self, order, now):
        if not order["filled"]:
            return order
        previous = next((o for o in self.store.orders() if o["id"] == order["id"]), None)
        settlement = (previous or {}).get("info", {}).get("settlementDate")
        if not settlement:
            settlement = next(
                (
                    i["settlement_date"]
                    for i in self.store.intents()
                    if i["order_id"] == order["id"] and i["kind"] == "create"
                ),
                None,
            )
        if (
            previous
            and previous["filled"] == order["filled"]
            and previous["cost"] == order["cost"]
            and previous["fee"]
        ):
            order["fee"] = previous["fee"]
            order["info"].update(
                {
                    key: value
                    for key, value in previous["info"].items()
                    if key in ("settlementDate", "costStatus", "estimatedCosts")
                }
            )
            order["info"]["observedAt"] = now
            return order
        cost = estimate_cost(
            self.options.get("costs", {}),
            order["symbol"],
            order["side"],
            1,
            order["cost"],
            dt_from_ts(order["timestamp"]),
        )
        order["fee"] = {k: cost[k] for k in ("currency", "cost", "rate")}
        order["info"].update(
            costStatus="estimated",
            estimatedCosts=cost,
            observedAt=now,
            settlementDate=settlement
            or self._settlement_date(order["info"]["businessDate"]).isoformat(),
        )
        return order

    def _raw_orders(self, start: date, end: date):
        if not 0 <= (end - start).days <= 365:
            raise ccxt.BadRequest("kis requires reconciliation gaps of at most 365 days")
        cutoff = dt_now().astimezone(SEOUL).date() - relativedelta(months=3)
        pages = []
        for first, last, tr_id in (
            (start, min(end, cutoff - timedelta(days=1)), "VTSC9215R"),
            (max(start, cutoff), end, "VTTC0081R"),
        ):
            if first > last:
                continue
            pages.extend(
                self.client.pages(
                    "trading/inquire-daily-ccld",
                    tr_id,
                    self._account()
                    | {
                        "INQR_STRT_DT": first.strftime("%Y%m%d"),
                        "INQR_END_DT": last.strftime("%Y%m%d"),
                        "SLL_BUY_DVSN_CD": "00",
                        "CCLD_DVSN": "00",
                        "INQR_DVSN": "01",
                        "INQR_DVSN_3": "00",
                        "PDNO": "",
                        "ORD_GNO_BRNO": "",
                        "ODNO": "",
                        "INQR_DVSN_1": "",
                        "CTX_AREA_FK100": "",
                        "CTX_AREA_NK100": "",
                        "EXCG_ID_DVSN_CD": "ALL",
                    },
                )
            )
        found: dict[str, dict] = {}
        for page in pages:
            if not isinstance(page.get("output1"), list):
                raise ccxt.ExchangeError("kis order page is incomplete")
            for row in page["output1"]:
                order = parse_order(row, self.scope)
                if not start <= date.fromisoformat(order["info"]["businessDate"]) <= end:
                    raise ccxt.ExchangeError("kis returned an order outside the requested dates")
                if order["id"] in found and found[order["id"]] != order:
                    raise ccxt.ExchangeError("kis order pages contain conflicting duplicates")
                found[order["id"]] = order
        return list(found.values())

    def _balance_snapshot(self):
        pages = self.client.pages(
            "trading/inquire-balance",
            "VTTC8434R",
            self._account()
            | {
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        positions, summary = {}, None
        for page in pages:
            items, current = parse_balance_page(page)
            if summary is not None and summary != current:
                raise ccxt.ExchangeError("kis balance changed between pages")
            summary = current
            if set(positions) & set(items):
                raise ccxt.ExchangeError("kis duplicate stock balance across pages")
            positions.update(items)
        if summary is None or summary["tot_loan_amt"]:
            raise ccxt.ExchangeError("kis requires a complete cash-only account snapshot")
        return {code: wallet for code, wallet in positions.items() if wallet["total"]}, summary

    def _capacity(self, symbol):
        data, _ = self.client.get(
            "trading/inquire-psbl-order",
            "VTTC8908R",
            self._account()
            | {
                "PDNO": symbol.removesuffix("/KRW"),
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        try:
            return tuple(
                int(number(data["output"][k], integer=True))
                for k in ("nrcvb_buy_amt", "nrcvb_buy_qty")
            )
        except (KeyError, TypeError):
            raise ccxt.ExchangeError("kis missing non-credit buying capacity") from None

    def _settlement_date(self, day):
        calendar = self.options.get("settlementCalendar", {})
        count, current = 0, date.fromisoformat(day)
        for _ in range(31):
            current += timedelta(days=1)
            record = calendar.get(current.isoformat())
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("settles"), bool)
                or not isinstance(record.get("source"), str)
                or not record["source"].strip()
            ):
                raise ccxt.ExchangeError("kis requires a verified broker settlement calendar")
            count += record["settles"]
            if count == 2:
                return current
        raise ccxt.ExchangeError("kis cannot establish the settlement date")

    def _recover(self, orders, start):
        claimed = {i["order_id"] for i in self.store.intents() if i["order_id"]}
        returned = {o["id"] for o in orders}
        expected = {
            i["order_id"]
            for i in self.store.intents()
            if i["order_id"]
            and (
                i["state"] in ("unknown", "accepted")
                or dt_from_ts(i["requested_at"]).astimezone(SEOUL).date() >= start
            )
        }
        if expected - returned:
            raise ccxt.ExchangeError("kis accepted orders are missing from the complete inquiry")
        for intent in self.store.intents():
            if intent["kind"] != "create" or intent["state"] != "unknown":
                continue
            candidates = [
                o
                for o in orders
                if o["id"] not in claimed
                and o["symbol"] == intent["symbol"]
                and o["side"] == intent["side"]
                and o["type"] == intent["order_type"]
                and o["amount"] == intent["amount"]
                and o["price"] == intent["price"]
                and intent["requested_at"] - 5000
                <= o["timestamp"]
                <= intent["requested_at"] + 35000
            ]
            if len(candidates) > 1:
                self.store.block("ambiguous_order_intent")
                raise ccxt.ExchangeError("kis cannot uniquely reconcile the transmitted request")
            if candidates:
                identifier = candidates[0]["id"]
                self.store.update_intent(intent["id"], "accepted", identifier)
                claimed.add(identifier)
        for order in orders:
            if order["id"] not in claimed:
                self.store.block("external_order")
                continue
            self.store.observe(self._fee(order, dt_ts()), dt_ts())
            if order["status"] != "open":
                for intent in self.store.intents():
                    if intent["order_id"] == order["id"]:
                        self.store.update_intent(intent["id"], "resolved")

    def _audit_start(self):
        baseline = self.store.baseline()
        start = date.fromisoformat(baseline.get("audit_day", baseline["day"]))
        for intent in self.store.intents():
            if intent["state"] in ("unknown", "accepted"):
                start = min(start, dt_from_ts(intent["requested_at"]).astimezone(SEOUL).date())
        return start

    def _stable_snapshot(self, start, end):
        previous = (self._raw_orders(start, end), self._balance_snapshot())
        for _ in range(3):
            current = (self._raw_orders(start, end), self._balance_snapshot())
            if current == previous:
                return current
            previous = current
        raise ccxt.ExchangeError(
            "kis account changed during reconciliation; refresh before trading"
        )

    def reconcile(self):
        with self._operation():
            today = dt_now().astimezone(SEOUL).date()
            baseline = self.store.baseline()
            if baseline is None:
                history, (positions, summary) = self._stable_snapshot(
                    today - timedelta(days=30), today
                )
                recent = [
                    o
                    for o in history
                    if o["status"] == "open"
                    or (o["filled"] and self._settlement_date(o["info"]["businessDate"]) >= today)
                ]
                if (
                    positions
                    or recent
                    or len(
                        {
                            summary[k]
                            for k in ("dnca_tot_amt", "nxdy_excc_amt", "prvs_rcdl_excc_amt")
                        }
                    )
                    != 1
                ):
                    raise ccxt.PermissionDenied(
                        "kis initial account must have no holdings, working orders "
                        "or unsettled activity"
                    )
                self.store.seed(summary["dnca_tot_amt"], today.isoformat())
                baseline = self.store.baseline()
            start = self._audit_start()
            history, (positions, summary) = self._stable_snapshot(start, today)
            self._recover(history, start)
            orders = self.store.orders()
            expected_positions = {}
            total, settled, unsettled = baseline["cash"], baseline["cash"], 0
            for order in orders:
                code = order["symbol"].removesuffix("/KRW")
                sign = 1 if order["side"] == "buy" else -1
                expected_positions[code] = expected_positions.get(code, 0) + sign * order["filled"]
                fee = order["fee"]["cost"] if order["filled"] else 0
                flow = -order["cost"] - fee if sign == 1 else order["cost"] - fee
                total += flow
                if order["filled"] and date.fromisoformat(order["info"]["settlementDate"]) <= today:
                    settled += flow
                elif sign == -1:
                    unsettled += flow
            expected_positions = {k: v for k, v in expected_positions.items() if v}
            if (
                total < 0
                or summary["dnca_tot_amt"] != settled
                or expected_positions != {k: v["total"] for k, v in positions.items()}
            ):
                if total >= 0 and any(i["state"] == "unknown" for i in self.store.intents()):
                    raise ccxt.ExchangeError(
                        "kis transmitted request is not yet visible; refresh history"
                    )
                self.store.block("cash_or_position_difference")
                raise ccxt.ExchangeError("kis cash or positions do not match the bot ledger")
            self.store.audited_through(today.isoformat())
            return positions, summary, total, unsettled

    def fetch_balance(self):
        with self._operation():
            positions, summary, total, unsettled = self.reconcile()
            self.store.assert_consistent()
            symbols = self.options.get("symbols", [])
            if not symbols:
                raise ccxt.BadRequest("kis requires configured symbols for the account cash cap")
            caps = {symbol: self._capacity(symbol) for symbol in symbols}
            reserves = 0
            known = {o["id"]: o for o in self.store.orders()}
            for intent in self.store.intents():
                if (
                    intent["kind"] != "create"
                    or intent["side"] != "buy"
                    or intent["state"] in ("resolved", "rejected")
                ):
                    continue
                order = known.get(intent["order_id"])
                reserves += math.ceil(
                    intent["reserved"] * (order["remaining"] / intent["amount"] if order else 1)
                )
            free = max(0, min(total - reserves - unsettled, min(v[0] for v in caps.values())))
            balance = {
                "KRW": {"total": total, "free": free, "used": total - free},
                **positions,
                "info": {
                    "cashBaselineScoped": True,
                    "reserved": reserves,
                    "unsettledSales": unsettled,
                    "brokerSettlement": summary,
                    "costStatus": "estimated",
                    "observedAt": dt_ts(),
                },
            }
            for field in ("free", "used", "total"):
                balance[field] = {
                    code: wallet[field]
                    for code, wallet in balance.items()
                    if code != "info" and isinstance(wallet, dict) and field in wallet
                }
            return balance

    def fetch_orders(self, symbol=None, since=None, limit=None):
        with self._operation():
            baseline = self.store.baseline()
            if baseline is None:
                self.reconcile()
            else:
                start = self._audit_start()
                self._recover(self._raw_orders(start, dt_now().astimezone(SEOUL).date()), start)
            orders = sorted(self.store.orders(), key=lambda o: (o["timestamp"], o["id"]))
            result = [
                o
                for o in orders
                if (symbol is None or o["symbol"] == symbol)
                and (since is None or o["timestamp"] >= since)
            ]
            return result[:limit] if limit is not None else result

    def fetch_order(self, identifier, symbol=None):
        matches = [o for o in self.fetch_orders(symbol) if o["id"] == identifier]
        if not matches:
            raise ccxt.OrderNotFound("kis order was not returned by complete broker inquiry")
        return matches[0]

    def fetch_my_trades(self, symbol=None, since=None, limit=None, order=None):
        with self._operation():
            self.fetch_orders(symbol)
            return self.store.trades(symbol, since, limit, order)

    def _session(self):
        if self.client._closed:
            raise ccxt.ExchangeNotAvailable("kis account client is closed")
        if not self.client.paper_orders:
            raise ccxt.NotSupported("kis paper broker orders are not enabled")
        now = dt_now()
        day = now.astimezone(SEOUL).date()
        session = self.data.session(day)
        try:
            verified = datetime.fromisoformat(
                self.options["sessions"][day.isoformat()]["verified_at"]
            )
            if (
                not session
                or verified.tzinfo is None
                or not 0 <= (now - verified).total_seconds() <= 86400
                or not session.opening <= now < session.closing
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ccxt.InvalidOrder("kis mutations require a fresh verified open session") from None

    def create_order(self, symbol, kind, side, amount, price):
        with self._operation():
            self._session()
            quantity = int(number(amount, positive=True, integer=True))
            if (
                symbol not in self.options.get("symbols", [])
                or kind not in ("market", "limit")
                or side not in ("buy", "sell")
            ):
                raise ccxt.InvalidOrder(
                    "kis requires a configured cash security and market/limit order"
                )
            if kind == "limit":
                price = float(number(price, positive=True, integer=True))
                reference = price
            else:
                reference = float(number(price, positive=True)) if price is not None else None
                price = None
            wallet = self.fetch_balance()
            self.store.assert_consistent()
            if self.store.unimported():
                raise ccxt.ExchangeError("kis has recovered orders awaiting bot DB import")
            if any(o["symbol"] == symbol and o["status"] == "open" for o in self.store.orders()):
                raise ccxt.InvalidOrder("kis already has a working order for this security")
            cap, capacity = self._capacity(symbol)
            valuation = price if kind == "limit" else (math.ceil(cap / capacity) if capacity else 0)
            if side == "buy":
                if quantity > capacity:
                    raise ccxt.InsufficientFunds("kis non-credit buying quantity is insufficient")
                fees = estimate_cost(
                    self.options.get("costs", {}), symbol, side, quantity, valuation, dt_now()
                )
                reserved = quantity * valuation + fees["cost"]
                if reserved > wallet["KRW"]["free"]:
                    raise ccxt.InsufficientFunds(
                        "kis non-credit cash or buying quantity is insufficient"
                    )
            else:
                reserved = 0
                if quantity > wallet.get(symbol.removesuffix("/KRW"), {}).get("free", 0):
                    raise ccxt.InsufficientFunds("kis sellable whole shares are insufficient")
            self._settlement_date(dt_now().astimezone(SEOUL).date().isoformat())
            reference = reference or valuation
            if reference <= 0:
                reference = self.data.fetch_order_book(symbol.removesuffix("/KRW"), 1)["bids"][0][0]
            estimate_cost(
                self.options.get("costs", {}), symbol, side, quantity, reference, dt_now()
            )
            identifier = self.store.intent(
                "create",
                symbol,
                side,
                kind,
                quantity,
                price,
                reserved,
                dt_ts(),
                reference_price=reference,
                settlement_date=self._settlement_date(
                    dt_now().astimezone(SEOUL).date().isoformat()
                ).isoformat(),
            )
            try:
                self._session()
                result = self.client.post(
                    "trading/order-cash",
                    "VTTC0012U" if side == "buy" else "VTTC0011U",
                    self._account()
                    | {
                        "PDNO": symbol.removesuffix("/KRW"),
                        "ORD_DVSN": "00" if kind == "limit" else "01",
                        "ORD_QTY": str(quantity),
                        "ORD_UNPR": str(int(price)) if price else "0",
                        "EXCG_ID_DVSN_CD": "KRX",
                        "SLL_TYPE": "01" if side == "sell" else "",
                        "CNDT_PRIC": "",
                    },
                )
            except ccxt.InvalidOrder:
                self.store.update_intent(identifier, "rejected")
                raise
            try:
                output = result["output"]
                day = dt_now().astimezone(SEOUL).strftime("%Y%m%d")
                broker_id = order_id(self.scope, day, output["KRX_FWDG_ORD_ORGNO"], output["ODNO"])
                stamp = broker_timestamp(day, output["ORD_TMD"])
            except (KeyError, TypeError):
                raise ccxt.ExchangeError(
                    "kis accepted request has an unresolved identity"
                ) from None
            self.store.update_intent(identifier, "accepted", broker_id)
            order = {
                "id": broker_id,
                "symbol": symbol,
                "type": kind,
                "side": side,
                "timestamp": stamp,
                "datetime": dt_from_ts(stamp).isoformat(),
                "timeInForce": "DAY",
                "amount": quantity,
                "filled": 0,
                "remaining": quantity,
                "status": "open",
                "price": price,
                "average": None,
                "cost": 0,
                "fee": None,
                "trades": None,
                "info": {
                    "businessDate": day,
                    "branch": output["KRX_FWDG_ORD_ORGNO"],
                    "brokerOrderId": output["ODNO"],
                    "canceledQuantity": 0,
                    "rejectedQuantity": 0,
                    "costStatus": "unverified",
                    "settlementDate": self._settlement_date(day).isoformat(),
                },
            }
            self.store.observe(order, dt_ts())
            return order

    def cancel_order(self, identifier, symbol=None):
        with self._operation():
            self._session()
            order = self.fetch_order(identifier, symbol)
            if order["status"] != "open":
                return order
            if order["info"]["businessDate"] != dt_now().astimezone(SEOUL).strftime("%Y%m%d"):
                raise ccxt.InvalidOrder("kis prior-day DAY orders require broker reconciliation")
            self.store.assert_consistent()
            if any(
                i["kind"] == "cancel"
                and i["order_id"] == identifier
                and i["state"] in ("unknown", "accepted")
                for i in self.store.intents()
            ):
                raise ccxt.ExchangeError(
                    "kis cancellation is pending; reconcile instead of resending"
                )
            pages = self.client.pages(
                "trading/inquire-psbl-rvsecncl",
                "TTTC0084R",
                self._account()
                | {
                    "INQR_DVSN_1": "0",
                    "INQR_DVSN_2": "0",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": "",
                },
            )
            eligible = []
            for page in pages:
                if not isinstance(page.get("output"), list) or any(
                    not isinstance(row, dict) for row in page["output"]
                ):
                    raise ccxt.ExchangeError("kis incomplete cancellation eligibility response")
                eligible.extend(
                    r for r in page["output"] if r.get("odno") == order["info"]["brokerOrderId"]
                )
            if len(eligible) != 1:
                raise ccxt.ExchangeError("kis cannot establish unique cancellation eligibility")
            row = eligible[0]
            try:
                quantity = int(number(row["psbl_qty"], positive=True, integer=True))
                branch = row["ord_gno_brno"]
            except (KeyError, TypeError):
                raise ccxt.ExchangeError(
                    "kis incomplete cancellation eligibility response"
                ) from None
            if quantity > order["remaining"] or branch != order["info"]["branch"]:
                raise ccxt.ExchangeError(
                    "kis cancellation routing or quantity needs reconciliation"
                )
            intent = self.store.intent(
                "cancel",
                order["symbol"],
                order["side"],
                order["type"],
                quantity,
                order["price"],
                0,
                dt_ts(),
                identifier,
            )
            try:
                self._session()
                self.client.post(
                    "trading/order-rvsecncl",
                    "VTTC0013U",
                    self._account()
                    | {
                        "KRX_FWDG_ORD_ORGNO": branch,
                        "ORGN_ODNO": order["info"]["brokerOrderId"],
                        "ORD_DVSN": "00" if order["type"] == "limit" else "01",
                        "RVSE_CNCL_DVSN_CD": "02",
                        "ORD_QTY": str(quantity),
                        "ORD_UNPR": "0",
                        "QTY_ALL_ORD_YN": "Y",
                        "EXCG_ID_DVSN_CD": "KRX",
                    },
                )
            except ccxt.InvalidOrder:
                self.store.update_intent(intent, "rejected")
                raise
            self.store.update_intent(intent, "accepted")
            return self.fetch_order(identifier, order["symbol"])
