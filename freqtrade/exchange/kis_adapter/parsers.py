"""KIS public market responses converted without inferred broker facts."""

import re

import ccxt

from freqtrade.exchange.kis_adapter.rules import broker_timestamp, number


def parse_master(payload: bytes, board: str) -> list[dict]:
    """Parse the pinned KIS KOSPI/KOSDAQ layout, retaining actual asset classes."""
    layouts = {"KOSPI": (227, 50, 60), "KOSDAQ": (221, 45, 55)}
    if board not in layouts:
        raise ccxt.BadRequest("kis unsupported board")
    size, lot, flags = layouts[board]
    try:
        rows = payload.decode("cp949").splitlines()
    except UnicodeError:
        raise ccxt.ExchangeError("kis master encoding is invalid") from None
    if not rows:
        raise ccxt.ExchangeError("kis empty master")
    result, seen = [], set()
    for row in rows:
        if len(row) < 21 + size:
            raise ccxt.ExchangeError("kis truncated master row")
        code, isin, name = row[:9].strip(), row[9:21].strip(), row[21:-size].strip()
        footer = row[-size:]
        group = footer[:2]
        if group not in ("ST", "EF"):
            continue
        if not re.fullmatch(r"[0-9A-Z]{6}", code) or code in seen or not name or len(isin) != 12:
            raise ccxt.ExchangeError("kis invalid or duplicate master security")
        seen.add(code)
        unit = number(footer[lot : lot + 5].strip(), positive=True, integer=True)
        restrictions = footer[flags : flags + 3]
        if any(flag not in "YN" for flag in restrictions):
            raise ccxt.ExchangeError("kis invalid master restrictions")
        result.append(
            {
                "id": code,
                "symbol": code + "/KRW",
                "base": code,
                "quote": "KRW",
                "baseId": code,
                "quoteId": "KRW",
                "type": "spot",
                "spot": True,
                "margin": False,
                "swap": False,
                "future": False,
                "option": False,
                "contract": False,
                "active": restrictions == "NNN" and unit == 1,
                "precision": {"amount": 1.0, "price": 1.0},
                "limits": {
                    "amount": {"min": float(unit), "max": None},
                    "price": {"min": 1.0, "max": None},
                    "cost": {"min": None, "max": None},
                },
                "info": {
                    "assetClass": "stock" if group == "ST" else "etf",
                    "board": board,
                    "name": name,
                    "isin": isin,
                    "securityGroup": group,
                    "halted": restrictions[0] == "Y",
                    "liquidation": restrictions[1] == "Y",
                    "administrative": restrictions[2] == "Y",
                },
            }
        )
    if not result:
        raise ccxt.ExchangeError("kis master contains no supported securities")
    return result


def parse_ticker(payload: dict, symbol: str, observed_at: int) -> dict:
    """Normalize session statistics; report receive time as an observation only."""
    data = payload.get("output")
    if not isinstance(data, dict):
        raise ccxt.ExchangeError("kis missing ticker output")
    values = {
        key: float(number(data.get(field), positive=True))
        for key, field in (
            ("last", "stck_prpr"),
            ("open", "stck_oprc"),
            ("high", "stck_hgpr"),
            ("low", "stck_lwpr"),
        )
    }
    if (
        not values["low"]
        <= min(values["open"], values["last"])
        <= max(values["open"], values["last"])
        <= values["high"]
    ):
        raise ccxt.ExchangeError("kis inconsistent ticker price range")
    return {
        "symbol": symbol,
        "timestamp": None,
        "datetime": None,
        "bid": None,
        "bidVolume": None,
        "ask": None,
        "askVolume": None,
        "baseVolume": float(number(data.get("acml_vol"), integer=True)),
        "quoteVolume": float(number(data.get("acml_tr_pbmn"))),
        "percentage": float(number(data.get("prdy_ctrt"), signed=True)),
        **values,
        "info": {"broker": data, "observedAt": observed_at, "statisticsPeriod": "session"},
    }


def parse_order_book(payload: dict, symbol: str, observed_at: int, limit: int = 10) -> dict:
    """Validate sorted KRX levels; keep the date-less broker clock in info."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise ccxt.BadRequest("kis order book limit must be 1 through 10")
    data = payload.get("output1")
    if not isinstance(data, dict):
        raise ccxt.ExchangeError("kis missing order book output")
    sides = {}
    for side, price, volume, reverse in (
        ("asks", "askp", "askp_rsqn", False),
        ("bids", "bidp", "bidp_rsqn", True),
    ):
        levels = []
        for i in range(1, 11):
            p, q = number(data.get(price + str(i))), number(data.get(volume + str(i)), integer=True)
            if p == q == 0:
                continue
            if p == 0 or q == 0:
                raise ccxt.ExchangeError("kis inconsistent order book level")
            levels.append([float(p), float(q)])
        prices = [level[0] for level in levels]
        if prices != sorted(prices, reverse=reverse) or len(set(prices)) != len(prices):
            raise ccxt.ExchangeError("kis unsorted or duplicate order book levels")
        sides[side] = levels[:limit]
    return {
        "symbol": symbol,
        "timestamp": None,
        "datetime": None,
        "nonce": None,
        **sides,
        "info": {"broker": data, "observedAt": observed_at},
    }


def parse_candle(data: dict, *, daily: bool) -> list:
    """Convert a single broker bar; completion and session checks are separate."""
    timestamp = broker_timestamp(
        data.get("stck_bsop_date"), "000000" if daily else data.get("stck_cntg_hour")
    )
    prices = [
        float(number(data.get(k), positive=True))
        for k in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr" if daily else "stck_prpr")
    ]
    opening, high, low, closing = prices
    if not low <= min(opening, closing) <= max(opening, closing) <= high:
        raise ccxt.ExchangeError("kis inconsistent candle price range")
    volume = number(data.get("acml_vol" if daily else "cntg_vol"), integer=True)
    return [timestamp, *prices, float(volume)]
