"""Read KIS market data using explicit schedules and bounded history requests."""

import copy
import io
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

import ccxt

from freqtrade.exchange.kis_adapter.client import KisClient, _NoRedirect
from freqtrade.exchange.kis_adapter.parsers import (
    parse_candle,
    parse_master,
    parse_order_book,
    parse_ticker,
)
from freqtrade.exchange.kis_adapter.rules import SEOUL, Session, broker_timestamp, number


@lru_cache(maxsize=4)
def _master_bytes(board: str, day: date) -> bytes:
    name = {"KOSPI": "kospi_code.mst", "KOSDAQ": "kosdaq_code.mst"}[board]
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        request = Request("https://new.real.download.dws.co.kr/common/master/" + name + ".zip")
        with opener.open(request, timeout=30) as response:
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ccxt.ExchangeError("kis master archive exceeds size limit")
        with ZipFile(io.BytesIO(data)) as archive:
            if archive.getinfo(name).file_size > 8_000_000:
                raise ccxt.ExchangeError("kis master exceeds size limit")
            return archive.read(name)
    except HTTPError as exc:
        exc.close()
        raise ccxt.NetworkError("kis master download failed") from None
    except (OSError, URLError, HTTPException):
        raise ccxt.NetworkError("kis master download failed") from None
    except (BadZipFile, KeyError, RuntimeError):
        raise ccxt.ExchangeError("kis master archive is invalid") from None


def complete_minutes(rows: list[list], session: Session, now: int, timeframe: str) -> list[list]:
    """Validate all completed continuous minutes and the isolated closing auction."""
    allowed = session.minute_starts()
    valid = set(allowed)
    by_time: dict[int, list] = {}
    for row in rows:
        timestamp = row[0]
        if timestamp not in valid:
            raise ccxt.ExchangeError("kis candle outside the verified session")
        if timestamp in by_time and by_time[timestamp] != row:
            raise ccxt.ExchangeError("kis conflicting duplicate candle")
        by_time[timestamp] = row
    completed = [t for t in allowed if t + 60_000 <= now]
    if any(t not in by_time for t in completed):
        raise ccxt.ExchangeError("kis missing completed intraday candle")
    if timeframe == "1m":
        return [by_time[t] for t in completed]
    if any(
        int((moment - session.opening).total_seconds()) % 300
        for moment in (session.auction, session.closing)
    ):
        raise ccxt.NotSupported("kis five-minute session boundaries are not aligned")
    result = []
    for i in range(0, len(allowed) - 1, 5):
        group = allowed[i : i + 5]
        if len(group) != 5 or allowed[-1] in group:
            raise ccxt.NotSupported("kis five-minute session boundaries are not aligned")
        if group[-1] + 60_000 > now:
            break
        bars = [by_time[t] for t in group]
        result.append(
            [
                group[0],
                bars[0][1],
                max(b[2] for b in bars),
                min(b[3] for b in bars),
                bars[-1][4],
                sum(b[5] for b in bars),
            ]
        )
    closing = allowed[-1]
    if closing + 300_000 <= now:
        result.append(by_time[closing])
    return result


class KisMarketData:
    """Own public-data caching and the configured calendar, without Freqtrade state."""

    def __init__(self, client: KisClient, options: dict) -> None:
        self.client = client
        if not isinstance(options, dict):
            raise ccxt.BadRequest("kis options must be a mapping")
        self._sessions = copy.deepcopy(options.get("sessions", {}))
        if not isinstance(self._sessions, dict):
            raise ccxt.BadRequest("kis sessions must be a date-keyed mapping")
        self._minute_cache: dict[tuple[str, date], list[list]] = {}

    def fetch_markets(self) -> list[dict]:
        """Load the daily KIS masters; never classify a code by its digits alone."""
        with self.client._lock:
            if self.client._closed:
                raise ccxt.ExchangeNotAvailable("kis client is closed")
            day = datetime.now(SEOUL).date()
            markets = [
                market
                for board in ("KOSPI", "KOSDAQ")
                for market in parse_master(_master_bytes(board, day), board)
            ]
        symbols = [m["symbol"] for m in markets]
        if len(symbols) != len(set(symbols)):
            raise ccxt.ExchangeError("kis duplicate security across boards")
        return markets

    def fetch_ticker(self, code: str) -> dict:
        """Read broker session statistics, keeping exchange time unknown."""
        data, _ = self.client.get(
            "quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        ticker = parse_ticker(data, code + "/KRW", int(time.time() * 1000))
        book = self.fetch_order_book(code, 1)
        for side, field in (("bids", "bid"), ("asks", "ask")):
            if book[side]:
                ticker[field], ticker[field + "Volume"] = book[side][0]
        ticker["info"]["orderBookObservedAt"] = book["info"]["observedAt"]
        return ticker

    def fetch_order_book(self, code: str, limit: int | None) -> dict:
        """Read and validate up to ten KRX order-book levels."""
        data, _ = self.client.get(
            "quotations/inquire-asking-price-exp-ccn",
            "FHKST01010200",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        return parse_order_book(
            data, code + "/KRW", int(time.time() * 1000), 10 if limit is None else limit
        )

    def session(self, day: date) -> Session | None:
        """Fail on unknown dates; an explicit null record represents a verified closure."""
        key = day.isoformat()
        if key not in self._sessions:
            raise ccxt.ExchangeNotAvailable("kis date is missing from the verified calendar")
        record = self._sessions[key]
        return None if record is None else Session.from_config(day, record)

    def _daily(self, code: str, start: date, end: date) -> dict[date, list]:
        result: dict[date, list] = {}
        cursor = end
        for _ in range(10):
            payload, _ = self.client.get(
                "quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "1",
                },
            )
            rows = payload.get("output2")
            if not isinstance(rows, list):
                raise ccxt.ExchangeError("kis missing daily candle output")
            if not rows:
                return result
            oldest = cursor + timedelta(days=1)
            for item in rows:
                timestamp = broker_timestamp(item.get("stck_bsop_date"))
                day = datetime.fromtimestamp(timestamp / 1000, SEOUL).date()
                if not start <= day <= cursor:
                    raise ccxt.ExchangeError("kis daily candle outside requested date range")
                oldest = min(oldest, day)
                if day == datetime.now(SEOUL).date():
                    continue
                bar = parse_candle(item, daily=True)
                if (
                    item.get("mod_yn") != "N"
                    or item.get("flng_cls_code") != "00"
                    or number(item.get("prtt_rate"), signed=True) != 0
                ):
                    raise ccxt.NotSupported("kis corporate-action candle requires reconciliation")
                if day in result and result[day] != bar:
                    raise ccxt.ExchangeError("kis conflicting duplicate daily candle")
                result[day] = bar
            if oldest == start:
                return result
            cursor = oldest - timedelta(days=1)
        raise ccxt.ExchangeError("kis daily pagination limit reached")

    def _read_minutes(self, code: str, day: date, session: Session, now: datetime) -> list[list]:
        paper = self.client.environment == "paper"
        if paper and day != now.date():
            raise ccxt.NotSupported("kis paper intraday history is limited to the current day")
        cursor = min(now, session.closing) if day == now.date() else session.closing
        result: dict[int, list] = {}
        for _ in range(100):
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_HOUR_1": cursor.strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "N",
            }
            if paper:
                path, tr = "quotations/inquire-time-itemchartprice", "FHKST03010200"
                params["FID_ETC_CLS_CODE"] = ""
            else:
                path, tr = "quotations/inquire-time-dailychartprice", "FHKST03010230"
                params.update(FID_INPUT_DATE_1=day.strftime("%Y%m%d"), FID_FAKE_TICK_INCU_YN="")
            payload, _ = self.client.get(path, tr, params)
            rows = payload.get("output2")
            if not isinstance(rows, list):
                raise ccxt.ExchangeError("kis missing minute candle output")
            if not rows:
                break
            oldest = cursor + timedelta(minutes=1)
            for item in rows:
                bar = parse_candle(item, daily=False)
                moment = datetime.fromtimestamp(bar[0] / 1000, SEOUL)
                if moment.date() != day or moment > cursor:
                    raise ccxt.ExchangeError("kis minute candle outside requested cursor")
                if bar[0] in result and result[bar[0]] != bar:
                    raise ccxt.ExchangeError("kis conflicting duplicate minute candle")
                result[bar[0]] = bar
                oldest = min(oldest, moment)
            if oldest <= session.opening:
                break
            cursor = oldest - timedelta(minutes=1)
        else:
            raise ccxt.ExchangeError("kis minute pagination limit reached")
        return list(result.values())

    def _minutes(self, code: str, day: date, session: Session, now: datetime) -> list[list]:
        cache_key = (code, day)
        with self.client._lock:
            if cache_key in self._minute_cache:
                return self._minute_cache[cache_key]
        complete = complete_minutes(
            self._read_minutes(code, day, session, now), session, int(now.timestamp() * 1000), "1m"
        )
        if day < now.date():
            daily = self._daily(code, day, day).get(day)
            if (
                not daily
                or not complete
                or complete[0][1] != daily[1]
                or complete[-1][4] != daily[4]
            ):
                raise ccxt.ExchangeError(
                    "kis minute prices do not match the unadjusted daily basis"
                )
            if max(b[2] for b in complete) > daily[2] or min(b[3] for b in complete) < daily[3]:
                raise ccxt.ExchangeError("kis minute prices exceed the unadjusted daily range")
            with self.client._lock:
                if len(self._minute_cache) >= 16:
                    self._minute_cache.pop(next(iter(self._minute_cache)))
                self._minute_cache[cache_key] = complete
        return complete

    def _history_calendar(self, since: int | None, now: datetime) -> list[date]:
        if since is not None and (
            isinstance(since, bool) or not isinstance(since, int) or since < 0
        ):
            raise ccxt.BadRequest("kis since must be nonnegative UTC milliseconds")
        if since is not None and since > now.timestamp() * 1000:
            return []
        try:
            first = (
                datetime.fromtimestamp(since / 1000, SEOUL).date()
                if since is not None
                else min(date.fromisoformat(key) for key in self._sessions)
            )
        except (ValueError, OverflowError, OSError):
            raise ccxt.BadRequest("kis requires a verified history calendar") from None
        if first < now.date() - timedelta(days=365):
            raise ccxt.NotSupported("kis initial history window is limited to 365 days")
        days = [first + timedelta(days=i) for i in range((now.date() - first).days + 1)]
        return days

    def _completed_daily(self, code: str, days: list[date], today: date):
        sessions = [(day, self.session(day)) for day in days]
        candles = self._daily(code, days[0], today)
        expected = [day for day, session in sessions if session is not None and day < today]
        if any(day not in candles for day in expected):
            raise ccxt.ExchangeError("kis missing completed daily candle")
        if any(day in candles for day, session in sessions if session is None):
            raise ccxt.ExchangeError("kis daily candle contradicts the verified calendar")
        return [candles[day] for day in expected]

    def fetch_ohlcv(
        self, code: str, timeframe: str, since: int | None, limit: int | None
    ) -> list[list]:
        """Return completed, ordered bars or fail on calendar/history gaps."""
        if timeframe not in ("1m", "5m", "1d"):
            raise ccxt.NotSupported("kis supports only 1m, 5m and 1d candles")
        limit = 500 if limit is None else limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ccxt.BadRequest("kis candle limit must be 1 through 10000")
        now = datetime.now(SEOUL)
        days = self._history_calendar(since, now)
        if not days:
            return []
        if (
            self.client.environment == "paper"
            and timeframe != "1d"
            and since is not None
            and any(self.session(day) is not None and day < now.date() for day in days)
        ):
            raise ccxt.NotSupported("kis paper intraday history is limited to the current day")
        if timeframe == "1d":
            result = self._completed_daily(code, days, now.date())
        else:
            result = []
            ordered_days = reversed(days) if since is None else days
            for day in ordered_days:
                session = self.session(day)
                if session is None or now < session.opening:
                    continue
                rows = self._minutes(code, day, session, now)
                bars = complete_minutes(rows, session, int(now.timestamp() * 1000), timeframe)
                if since is None:
                    result = bars + result
                else:
                    result.extend(bar for bar in bars if bar[0] >= since)
                if len(result) >= limit:
                    break
        if since is not None:
            return [bar.copy() for bar in result if bar[0] >= since][:limit]
        if len(result) < limit:
            raise ccxt.ExchangeError("kis insufficient configured candle history")
        return [bar.copy() for bar in result[-limit:]]
