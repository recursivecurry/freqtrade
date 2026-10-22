"""Experimental KIS cash-equity integration with virtual and guarded paper orders."""

import copy
import logging
from datetime import UTC, datetime, time, timedelta

import ccxt

from freqtrade.enums import CandleType, RunMode
from freqtrade.exceptions import (
    ConfigurationError,
    InvalidOrderException,
    OperationalException,
    TemporaryError,
)
from freqtrade.exchange.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas
from freqtrade.exchange.kis_adapter import KisAsync, KisSync
from freqtrade.exchange.kis_adapter.costs import estimate_cost
from freqtrade.exchange.kis_adapter.rules import (
    SEOUL,
    broker_timestamp,
    number,
    round_price,
    tick_size,
)
from freqtrade.util import dt_from_ts, dt_now, dt_ts


logger = logging.getLogger(__name__)


class Kis(Exchange):
    """Keep production writes disabled while integrating KRX cash-equity accounting."""

    _ft_has: FtHas = {
        "always_require_api_keys": True,
        "order_time_in_force": ["DAY"],
        "ohlcv_partial_candle": False,
        "ohlcv_fill_missing": False,
        "ohlcv_align_timestamps": False,
        "price_band_precision": True,
        "ohlcv_candle_limit": 500,
        "l2_limit_range": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "l2_limit_upper": 10,
        "download_data_parallel_quick": False,
        "marketOrderRequiresPrice": True,
        "include_open_fills": True,
    }

    def __init__(self, config, **kwargs):
        exchange_config = kwargs.get("exchange_config") or config["exchange"]
        self._paper_enabled = not config.get("dry_run", True) and config.get("runmode") in (
            RunMode.LIVE,
            RunMode.DRY_RUN,
        )
        if self._paper_enabled and exchange_config.get("kis", {}).get("environment") != "paper":
            raise ConfigurationError("KIS broker writes currently require paper mode")
        if any(
            exchange_config.get(key)
            for key in ("ccxt_config", "ccxt_sync_config", "ccxt_async_config", "_ft_has_params")
        ):
            raise ConfigurationError(
                "KIS uses exchange.kis settings; CCXT overrides are unsupported"
            )
        if exchange_config.get("log_responses"):
            raise ConfigurationError("KIS raw response logging is disabled")
        self._kis_options = copy.deepcopy(exchange_config.get("kis", {}))
        if not isinstance(self._kis_options, dict):
            raise ConfigurationError("KIS settings must be a mapping")
        self._cost_profiles = self._kis_options.get("costs", {})
        try:
            super().__init__(config, **kwargs)
        except BaseException:
            self.close()
            raise

    def _init_ccxt(self, exchange_config, sync, ccxt_kwargs):
        config = {
            "apiKey": exchange_config.get("api_key", exchange_config.get("key")),
            "secret": exchange_config.get("secret"),
            "accountId": exchange_config.get("account_id") or "",
            "uid": exchange_config.get("uid") or "",
            "options": {
                key: self._kis_options[key]
                for key in ("environment", "sessions", "costs")
                if key in self._kis_options
            }
            | {
                "enablePaperOrders": self._paper_enabled,
                "dbUrl": self._config.get("db_url", ""),
                "symbols": exchange_config.get("pair_whitelist", []),
                "settlementCalendar": self._kis_options.get("settlement_calendar", {}),
            },
        }
        try:
            if sync:
                return KisSync(config)
            return KisAsync(config, client=self._api._client)
        except ccxt.BaseError as exc:
            raise ConfigurationError(f"KIS client configuration failed: {exc}") from None

    def close(self):
        """Release both local facades, including the synchronous reference."""
        if hasattr(self, "_exchange_ws"):
            super().close()
        if isinstance(getattr(self, "_api", None), KisSync):
            self._api.close()

    def validate_config(self, config):
        """Require cash spot, explicit costs and DAY orders before virtual trading."""
        super().validate_config(config)
        if config.get("fee") is not None:
            raise ConfigurationError("KIS requires dated symbol costs instead of a symmetric fee")
        if any(p["method"] != "StaticPairList" for p in config.get("pairlists", [])):
            raise ConfigurationError("KIS supports StaticPairList only")
        if config.get("freqai", {}).get("enabled") or config.get("use_public_trades"):
            raise ConfigurationError("KIS FreqAI and public order-flow data are unsupported")
        if config.get("startup_candle_count", 0) > 9998:
            raise ConfigurationError("KIS supports at most 9998 startup candles")
        if self._paper_enabled and (
            config.get("position_adjustment_enable")
            or config["exchange"].get("skip_open_order_update")
        ):
            raise ConfigurationError(
                "KIS paper trading requires startup reconciliation and no position adjustment"
            )
        for symbol in config["exchange"].get("pair_whitelist", []):
            if symbol not in self.markets or not self.markets[symbol]["active"]:
                raise ConfigurationError("KIS requires explicitly listed, active securities")
            if config.get("runmode") in (
                RunMode.LIVE,
                RunMode.DRY_RUN,
            ):
                self.estimate_fee_rate(symbol, "buy", 1, 10000, dt_now(), 0)
                self.estimate_fee_rate(symbol, "sell", 1, 10000, dt_now(), 0)

    def validate_ordertypes(self, order_types):
        """Allow virtual market/limit orders, with no broker stop-loss order."""
        self.validate_stop_ordertypes(order_types)
        if any(
            value not in ("market", "limit")
            for key, value in order_types.items()
            if key in ("entry", "exit", "stoploss", "emergency_exit", "force_entry", "force_exit")
        ):
            raise ConfigurationError("KIS supports market and limit order types only")

    def validate_order_time_in_force(self, order_time_in_force):
        """Require both sides to use the broker's DAY lifetime."""
        if set(order_time_in_force) != {"entry", "exit"} or any(
            value.upper() != "DAY" for value in order_time_in_force.values()
        ):
            raise ConfigurationError("KIS entry and exit time-in-force must be DAY")

    def ohlcv_candle_limit(self, timeframe, candle_type, since_ms=None):
        """Ask for the strategy's required bars, avoiding unnecessary paper-day history."""
        return max(
            2,
            getattr(self, "_startup_candle_count", self._config.get("startup_candle_count", 0)) + 2,
        )

    def price_to_precision(self, pair, price, *, rounding_mode=ccxt.ROUND):
        """Round against current asset-specific KRX price bands."""
        return float(round_price(price, self.markets[pair]["info"]["assetClass"], rounding_mode))

    def price_get_one_pip(self, pair, price):
        """Return the legal tick at this price rather than the nominal metadata unit."""
        return float(
            tick_size(number(price, positive=True), self.markets[pair]["info"]["assetClass"])
        )

    def can_place_order(self, pair: str, when: datetime) -> bool:
        """Block new orders outside a fresh, explicitly verified session."""
        day = when.astimezone(SEOUL).date()
        try:
            session = self._api._data.session(day)
            if session is None:
                return False
            if self._config.get("runmode") in (RunMode.LIVE, RunMode.DRY_RUN):
                record = self._kis_options["sessions"][day.isoformat()]
                verified = datetime.fromisoformat(record["verified_at"])
                if verified.tzinfo is None or not 0 <= (when - verified).total_seconds() <= 86400:
                    return False
            return session.opening <= when < session.closing
        except (ccxt.BaseError, ValueError, TypeError, KeyError):
            self.log_once(
                "KIS orders blocked: session schedule is missing or stale", logger.warning
            )
            return False

    def candle_execution_time(self, timeframe: str, when: datetime) -> datetime:
        """Execute a daily broker candle at that business date's actual opening."""
        if timeframe != "1d" or when.astimezone(SEOUL).time() != time.min:
            return when
        session = self._api._data.session(when.astimezone(SEOUL).date())
        return session.opening.astimezone(UTC) if session else when

    def candle_close_time(self, timeframe, when, now):
        """Publish a daily signal at the following verified session's opening."""
        if timeframe != "1d":
            return super().candle_close_time(timeframe, when, now)
        try:
            if dt_ts(when) != self._daily_publication_cutoff(now):
                return None
            start = when.astimezone(SEOUL).date()
            end = now.astimezone(SEOUL).date()
            following = None
            for index in range(1, (end - start).days + 1):
                session = self._api._data.session(start + timedelta(days=index))
                if session and following is None:
                    following = session.opening
            return following
        except ccxt.BaseError:
            return None

    def order_expired(self, pair: str, created: datetime, when: datetime) -> bool:
        """Expire a DAY order at its close, including across a restart or date rollover."""
        day = created.astimezone(SEOUL).date()
        if when.astimezone(SEOUL).date() > day:
            return True
        session = self._api._data.session(day)
        return bool(session and when > session.closing)

    def validate_candle_data(self, pair, timeframe, dataframe):
        """Detect omitted intraday bars and closed-session rows in imported datasets."""
        if dataframe.empty:
            return
        actual = {dt_ts(value.to_pydatetime()) for value in dataframe["date"]}
        earliest, latest = min(actual), max(actual)
        start, end = (
            dt_from_ts(earliest).astimezone(SEOUL).date(),
            dt_from_ts(latest).astimezone(SEOUL).date(),
        )
        expected = set()
        for index in range((end - start).days + 1):
            day = start + timedelta(days=index)
            session = self._api._data.session(day)
            if session is None:
                continue
            labels = (
                [broker_timestamp(day.strftime("%Y%m%d"))]
                if timeframe == "1d"
                else session.minute_starts()
            )
            if timeframe == "5m":
                if any(
                    int((moment - session.opening).total_seconds()) % 300
                    for moment in (session.auction, session.closing)
                ):
                    raise OperationalException("KIS five-minute session boundaries are not aligned")
                labels = labels[::5]
            expected.update(value for value in labels if earliest <= value <= latest)
        if actual != expected:
            raise OperationalException("KIS dataset has a session gap or an out-of-session candle")

    def estimate_fee_rate(self, symbol, side, amount, price, when, fallback_rate):
        """Estimate each side separately, retaining explicit cost provenance."""
        try:
            return estimate_cost(self._cost_profiles, symbol, side, amount, price, when)["rate"]
        except ccxt.BaseError as exc:
            raise OperationalException(f"KIS cost model is unavailable: {exc}") from None

    def get_fee(self, symbol, order_type="", side="", amount=1, price=1, taker_or_maker="maker"):
        """Supply a conservative nominal rate to legacy sizing paths."""
        sides = (side,) if side else ("buy", "sell")
        try:
            dates = (
                [
                    datetime.fromisoformat(rule["from"]).replace(tzinfo=SEOUL)
                    for rule in self._cost_profiles[symbol]
                ]
                if self._config.get("runmode") in (RunMode.BACKTEST, RunMode.HYPEROPT)
                else [dt_now()]
            )
            return max(
                estimate_cost(self._cost_profiles, symbol, s, 1, 1_000_000, when)["nominalRate"]
                for s in sides
                for when in dates
            )
        except (ccxt.BaseError, KeyError, ValueError, TypeError) as exc:
            raise OperationalException(f"KIS cost model is unavailable: {exc}") from None

    def add_dry_order_fee(self, pair, dry_order, taker_or_maker):
        """Attach estimated commission and taxes once to the virtual fill."""
        fee = estimate_cost(
            self._cost_profiles,
            pair,
            dry_order["side"],
            dry_order["filled"],
            dry_order["average"],
            dt_now(),
        )
        dry_order["fee"] = {key: fee[key] for key in ("currency", "cost", "rate")}
        dry_order["info"]["estimatedCosts"] = fee
        return dry_order

    def create_order(self, **kwargs):
        """Require paper mode and a verified session before broker submission."""
        if not self._config["dry_run"] and not self._paper_enabled:
            raise InvalidOrderException("KIS broker orders require explicit paper mode")
        if kwargs.get("time_in_force", "DAY").upper() != "DAY":
            raise InvalidOrderException("KIS orders require DAY")
        if not self.can_place_order(kwargs["pair"], dt_now()):
            raise InvalidOrderException("KIS orders require a fresh open session")
        return super().create_order(**kwargs)

    def recover_order_intents(self):
        if not self._paper_enabled:
            return []
        try:
            self._api._trading.fetch_orders()
            return self._api._trading.store.unimported()
        except ccxt.BaseError as exc:
            raise TemporaryError(f"KIS intent reconciliation failed: {exc}") from None

    def get_trades_for_order(self, order_id, pair, since, params=None):
        return super().get_trades_for_order(
            order_id, pair, since, (params or {}) | {"order": order_id}
        )

    def acknowledge_order_intent(self, order):
        if self._paper_enabled:
            try:
                with self._api._trading._operation():
                    self._api._trading.store.mark_imported(order)
            except ccxt.BaseError as exc:
                raise TemporaryError(f"KIS intent acknowledgement failed: {exc}") from None

    def _update_trade_costs(self, trade):
        if not self._paper_enabled:
            return
        identifiers = {order.order_id for order in trade.orders}
        orders = [
            o for o in self._api._trading.store.orders() if o["id"] in identifiers and o["filled"]
        ]
        for side in (trade.entry_side, trade.exit_side):
            matching = [o for o in orders if o["side"] == side]
            if not matching or any(o["fee"] is None for o in matching):
                continue
            fee = sum(o["fee"]["cost"] for o in matching)
            gross = sum(o["cost"] for o in matching)
            suffix = "open" if side == trade.entry_side else "close"
            setattr(trade, "fee_" + suffix, fee / gross)
            setattr(trade, "fee_" + suffix + "_cost", fee)
            setattr(trade, "fee_" + suffix + "_currency", "KRW")

    def update_trade_from_order(self, trade, order, recalculating):
        if not self._paper_enabled:
            return super().update_trade_from_order(trade, order, recalculating)
        entries = [o for o in trade.orders if o.ft_order_side == "buy" and o.safe_filled]
        exits = [o for o in trade.orders if o.ft_order_side == "sell" and o.safe_filled]
        bought, sold = sum(o.safe_filled for o in entries), sum(o.safe_filled for o in exits)
        if sold > bought or (bought and bought == sold and order.status == "open"):
            raise OperationalException("KIS cumulative fills conflict with the owned position")
        self._update_trade_costs(trade)
        buy_fee, sell_fee = trade.fee_open_cost, trade.fee_close_cost
        trade.recalc_trade_from_orders(include_open=True)
        trade.fee_open_cost, trade.fee_close_cost = buy_fee, sell_fee
        if bought:
            buy_gross, sell_gross = sum(o.cost for o in entries), sum(o.cost for o in exits)
            trade.open_rate = buy_gross / bought
            trade.amount = bought - sold
            trade.stake_amount = trade.amount * trade.open_rate
            trade.max_stake_amount = buy_gross
            trade.recalc_open_trade_value()
            if sold:
                realized = (
                    sell_gross
                    - (trade.fee_close_cost or 0)
                    - (buy_gross + (trade.fee_open_cost or 0)) * sold / bought
                )
                trade.realized_profit = realized
                if bought == sold:
                    trade.amount, trade.stake_amount = bought, buy_gross
                    trade.close(order.safe_price)
                    trade.fee_open_cost, trade.fee_close_cost = buy_fee, sell_fee
                    trade.close_profit_abs = trade.realized_profit = realized
                    trade.close_profit = realized / (buy_gross + (trade.fee_open_cost or 0))
        trade.commit()

    def create_dry_run_order(
        self,
        pair,
        ordertype,
        side,
        amount,
        rate,
        leverage,
        params=None,
        stop_loss=False,
        stop_price=None,
    ):
        """Create a whole-share virtual order only during the verified session."""
        if not self.can_place_order(pair, dt_now()) or leverage != 1 or stop_loss:
            raise InvalidOrderException("KIS order blocked by session or cash-only policy")
        amount = self.amount_to_precision(pair, amount)
        if amount < 1:
            raise InvalidOrderException("KIS order requires at least one whole share")
        result = super().create_dry_run_order(pair, ordertype, side, amount, rate, leverage, params)
        result["timeInForce"] = "DAY"
        return result

    def check_dry_limit_order_filled(self, order, immediate=False, orderbook=None):
        """Expire DAY orders before checking prices; never fill outside the session."""
        if order["status"] != "open":
            return order
        now = dt_now()
        if self.order_expired(order["symbol"], dt_from_ts(order["timestamp"]), now):
            order.setdefault("info", {})
            order["info"]["expiredQuantity"] = order["remaining"]
            order.update(status="expired", remaining=0)
            return order
        if not self.can_place_order(order["symbol"], now):
            return order
        return super().check_dry_limit_order_filled(order, immediate, orderbook)

    def _build_coroutine(self, pair, timeframe, candle_type, since_ms, cache):
        if since_ms is not None:
            return self._async_get_historic_ohlcv(pair, timeframe, since_ms, candle_type, True)
        return self._async_get_candle_history(pair, timeframe, candle_type)

    def _daily_publication_cutoff(self, now):
        sessions = [
            self._api._data.session(datetime.fromisoformat(day).date())
            for day in sorted(self._kis_options.get("sessions", {}))
            if day <= now.astimezone(SEOUL).date().isoformat()
        ]
        opened = [session for session in sessions if session and session.opening <= now]
        return broker_timestamp(opened[-2].opening.strftime("%Y%m%d")) if len(opened) >= 2 else None

    def _now_is_time_to_refresh(self, pair, timeframe, candle_type):
        now = dt_now()
        if timeframe == "1d":
            cutoff = self._daily_publication_cutoff(now)
            return cutoff is not None and cutoff > self._pairs_last_refresh_time.get(
                (pair, timeframe, candle_type), 0
            )
        session = self._api._data.session(now.astimezone(SEOUL).date())
        if not session or now < session.opening or dt_ts(now) > dt_ts(session.closing) + 310_000:
            return False
        return super()._now_is_time_to_refresh(pair, timeframe, candle_type)

    async def _async_get_candle_history(self, pair, timeframe, candle_type, since_ms=None):
        result = await super()._async_get_candle_history(pair, timeframe, candle_type, since_ms)
        if timeframe == "1d" and since_ms is None:
            cutoff = self._daily_publication_cutoff(dt_now())
            rows = [row for row in result[3] if cutoff is not None and row[0] <= cutoff]
            return pair, timeframe, candle_type, rows, False
        return result

    async def _async_get_historic_ohlcv(
        self, pair, timeframe, since_ms, candle_type, raise_=False, until_ms=None
    ):
        if candle_type != CandleType.SPOT:
            raise OperationalException("KIS supports spot candles only")
        cursor, end, data = since_ms, until_ms or dt_ts(), []
        while cursor < end:
            _, _, _, rows, _ = await super()._async_get_candle_history(
                pair, timeframe, candle_type, cursor
            )
            if not rows:
                break
            if rows[-1][0] < cursor:
                raise OperationalException("KIS history cursor did not advance")
            data.extend(row for row in rows if row[0] < end)
            cursor = rows[-1][0] + 1
        return pair, timeframe, candle_type, data, False
