"""Durable paper intents and observation watermarks in the bot's SQLite database."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import ccxt
from filelock import FileLock, Timeout
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class KisStore:
    def __init__(self, url: str, scope: str):
        try:
            database = make_url(url)
            if (
                database.drivername != "sqlite"
                or not database.database
                or database.database == ":memory:"
                or database.query
            ):
                raise ValueError
        except (ArgumentError, ValueError, TypeError):
            raise ccxt.BadRequest(
                "kis paper trading requires a durable SQLite bot database"
            ) from None
        self.scope = scope
        filename = str(Path(database.database).resolve())
        self._owner = FileLock(filename + ".kis.lock")
        try:
            self._owner.acquire(timeout=0)
        except Timeout:
            raise ccxt.PermissionDenied(
                "kis database already has an active account client"
            ) from None
        try:
            self.db = sqlite3.connect(
                filename, timeout=5, isolation_level=None, check_same_thread=False
            )
            self.db.row_factory = sqlite3.Row
            if not self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='KeyValueStore'"
            ).fetchone():
                raise ccxt.BadRequest("kis requires an initialized Freqtrade database")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS kis_intents (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
                    symbol TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL,
                    amount INTEGER NOT NULL, price REAL, reserved REAL NOT NULL,
                    requested_at INTEGER NOT NULL, state TEXT NOT NULL, order_id TEXT,
                    reference_price REAL, settlement_date TEXT
                );
                CREATE TABLE IF NOT EXISTS kis_observations (
                    order_id TEXT PRIMARY KEY, scope TEXT NOT NULL, observed_at INTEGER NOT NULL,
                    filled INTEGER NOT NULL, gross INTEGER NOT NULL, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kis_fills (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                    order_id TEXT NOT NULL, observed_at INTEGER NOT NULL,
                    cumulative_qty INTEGER NOT NULL, trade TEXT NOT NULL,
                    UNIQUE(scope, order_id, cumulative_qty)
                );
            """)
            baseline = self.baseline()
            if baseline and baseline["scope"] != scope:
                raise ccxt.PermissionDenied(
                    "kis database belongs to another account or environment"
                )
        except BaseException:
            self.close()
            raise

    def close(self):
        if hasattr(self, "db"):
            self.db.close()
        self._owner.release()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _value(self, key):
        row = self.db.execute(
            "SELECT string_value FROM KeyValueStore WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _set_value(self, key, value):
        encoded = json.dumps(value, separators=(",", ":"))
        if len(encoded) > 255:
            raise ccxt.ExchangeError("kis account metadata exceeds the persistent field limit")
        row = self.db.execute("SELECT id FROM KeyValueStore WHERE key=?", (key,)).fetchone()
        if row:
            self.db.execute(
                "UPDATE KeyValueStore SET string_value=?,value_type=? WHERE id=?",
                (encoded, "str", row[0]),
            )
        else:
            self.db.execute(
                "INSERT INTO KeyValueStore(key,value_type,string_value) VALUES(?,?,?)",
                (key, "str", encoded),
            )

    def baseline(self):
        return self._value("kis_cash_baseline")

    def seed(self, cash: int, day: str):
        with self.transaction():
            if self.baseline():
                raise ccxt.ExchangeError("kis cash baseline already exists")
            self._set_value("kis_cash_baseline", {"scope": self.scope, "cash": cash, "day": day})

    def audited_through(self, day):
        with self.transaction():
            baseline = self.baseline()
            baseline["audit_day"] = day
            self._set_value("kis_cash_baseline", baseline)

    def block(self, reason: str):
        with self.transaction():
            if not self._value("kis_reconciliation_block") or reason in (
                "inconsistent_cumulative_snapshot",
                "canonical_fill_total_difference",
            ):
                self._set_value("kis_reconciliation_block", {"scope": self.scope, "reason": reason})

    def assert_consistent(self):
        if self._value("kis_reconciliation_block"):
            raise ccxt.ExchangeError("kis account requires reconciliation; new orders are blocked")

    def intent(
        self,
        kind,
        symbol,
        side,
        order_type,
        amount,
        price,
        reserved,
        now,
        order=None,
        reference_price=None,
        settlement_date=None,
    ):
        identifier = uuid.uuid4().hex
        with self.transaction():
            self.assert_consistent()
            if self.db.execute("SELECT 1 FROM kis_intents WHERE state='unknown'").fetchone():
                raise ccxt.ExchangeError("kis has an unresolved request; never resend it")
            self.db.execute(
                "INSERT INTO kis_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    self.scope,
                    kind,
                    symbol,
                    side,
                    order_type,
                    amount,
                    price,
                    reserved,
                    now,
                    "unknown",
                    order,
                    reference_price if reference_price is not None else price,
                    settlement_date,
                ),
            )
        return identifier

    def update_intent(self, identifier, state, order=None):
        with self.transaction():
            self.db.execute(
                "UPDATE kis_intents SET state=?,order_id=COALESCE(?,order_id) WHERE id=?",
                (state, order, identifier),
            )

    def intents(self):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM kis_intents WHERE scope=? ORDER BY requested_at,id", (self.scope,)
            )
        ]

    def orders(self):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT snapshot FROM kis_observations WHERE scope=? ORDER BY order_id",
                (self.scope,),
            )
        ]

    def observe(self, order: dict, now: int):
        mismatch = False
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM kis_observations WHERE order_id=?", (order["id"],)
            ).fetchone()
            previous = json.loads(row["snapshot"]) if row else None
            qty, gross = order["filled"], int(order["cost"])
            old_qty, old_gross = (row["filled"], row["gross"]) if row else (0, 0)
            dq, dc = qty - old_qty, gross - old_gross
            mismatch = (
                dq < 0
                or dc < 0
                or bool(dq) != bool(dc)
                or (row is not None and now < row["observed_at"])
                or (
                    previous is not None
                    and any(
                        previous[k] != order[k]
                        for k in ("symbol", "side", "type", "amount", "timestamp")
                    )
                )
                or (
                    previous is not None
                    and previous["status"] != "open"
                    and any(previous[k] != order[k] for k in ("status", "filled", "cost"))
                )
                or (
                    previous is not None
                    and any(
                        previous["info"][k] > order["info"][k]
                        for k in ("canceledQuantity", "rejectedQuantity")
                    )
                )
            )
            if not mismatch:
                if dq:
                    trade = {
                        "order": order["id"],
                        "symbol": order["symbol"],
                        "side": order["side"],
                        "type": order["type"],
                        "timestamp": now,
                        "datetime": datetime.fromtimestamp(now / 1000, UTC).isoformat(),
                        "amount": dq,
                        "cost": dc,
                        "price": dc / dq,
                        "fee": None,
                        "info": {
                            "synthetic": True,
                            "timestamp_kind": "observation",
                            "intervalStart": row["observed_at"] if row else None,
                            "intervalEnd": now,
                            "recoveredAggregate": row is None,
                        },
                    }
                    result = self.db.execute(
                        "INSERT INTO kis_fills(scope,order_id,observed_at,cumulative_qty,trade) "
                        "VALUES(?,?,?,?,?)",
                        (self.scope, order["id"], now, qty, json.dumps(trade)),
                    )
                    trade["id"] = f"{self.scope}:fill:{result.lastrowid}"
                    self.db.execute(
                        "UPDATE kis_fills SET trade=? WHERE sequence=?",
                        (json.dumps(trade), result.lastrowid),
                    )
                self.db.execute(
                    "INSERT OR REPLACE INTO kis_observations VALUES(?,?,?,?,?,?)",
                    (order["id"], self.scope, now, qty, gross, json.dumps(order)),
                )
        if mismatch:
            self.block("inconsistent_cumulative_snapshot")
            raise ccxt.ExchangeError("kis cumulative order history requires reconciliation")
        self._check_totals()

    def _check_totals(self):
        fills = [
            json.loads(r[0])
            for r in self.db.execute("SELECT trade FROM kis_fills WHERE scope=?", (self.scope,))
        ]
        for order in self.orders():
            matching = [fill for fill in fills if fill["order"] == order["id"]]
            if (
                sum(f["amount"] for f in matching) != order["filled"]
                or sum(f["cost"] for f in matching) != order["cost"]
            ):
                self.block("canonical_fill_total_difference")
                raise ccxt.ExchangeError("kis canonical fill totals require reconciliation")

    def unimported(self):
        orders = {o["id"]: o for o in self.orders()}
        result = []
        for intent in self.intents():
            identifier = intent["order_id"]
            if intent["kind"] != "create" or identifier not in orders:
                continue
            if not self.db.execute(
                "SELECT 1 FROM orders WHERE order_id=?", (identifier,)
            ).fetchone():
                order = orders[identifier]
                order["info"]["intentReferencePrice"] = intent["reference_price"]
                result.append(order)
        return result

    def trades(self, symbol=None, since=None, limit=None, order=None):
        block = self._value("kis_reconciliation_block")
        if block and block["reason"] in (
            "inconsistent_cumulative_snapshot",
            "canonical_fill_total_difference",
        ):
            raise ccxt.ExchangeError("kis canonical history requires reconciliation")
        self._check_totals()
        rows = self.db.execute(
            "SELECT trade FROM kis_fills WHERE scope=? ORDER BY observed_at,sequence", (self.scope,)
        )
        result = [
            t
            for r in rows
            if (t := json.loads(r[0]))
            and (symbol is None or t["symbol"] == symbol)
            and (since is None or t["timestamp"] >= since)
            and (order is None or t["order"] == order)
        ]
        return result[:limit] if limit is not None else result
