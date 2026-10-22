"""Strict order totals; REST observations are not individual broker executions."""

import re
from datetime import UTC, datetime

import ccxt

from freqtrade.exchange.kis_adapter.rules import broker_timestamp, number, round_price


def validate_market_order(market, kind, price):
    if not market["active"]:
        raise ccxt.InvalidOrder("kis requires an active cash security")
    if kind == "limit":
        value = number(price, positive=True, integer=True)
        if round_price(value, market["info"]["assetClass"]) != str(int(value)):
            raise ccxt.InvalidOrder("kis limit price does not satisfy the security's tick rule")


def parse_balance_page(page):
    try:
        if (
            not isinstance(page["output1"], list)
            or not isinstance(page["output2"], list)
            or len(page["output2"]) != 1
        ):
            raise ValueError
        summary = {
            key: int(number(page["output2"][0][key], integer=True))
            for key in ("dnca_tot_amt", "nxdy_excc_amt", "prvs_rcdl_excc_amt", "tot_loan_amt")
        }
        positions = {}
        for row in page["output1"]:
            code = row["pdno"]
            total, free = [
                int(number(row[key], integer=True)) for key in ("hldg_qty", "ord_psbl_qty")
            ]
            if not re.fullmatch(r"[A-Z0-9]{6}", code) or free > total or code in positions:
                raise ValueError
            positions[code] = {"total": total, "free": free, "used": total - free}
        return positions, summary
    except (KeyError, ValueError, TypeError):
        raise ccxt.ExchangeError("kis incomplete or inconsistent balance page") from None


def order_id(scope: str, day: str, branch: str, broker_id: str) -> str:
    if not (
        re.fullmatch(r"[0-9]{8}", day)
        and re.fullmatch(r"[0-9]{1,10}", broker_id)
        and re.fullmatch(r"[0-9]{1,10}", branch)
    ):
        raise ccxt.ExchangeError("kis invalid broker order identity")
    return f"{scope}:{day}:{branch}:{broker_id}"


def parse_order(row: dict, scope: str) -> dict:
    try:
        code = row["pdno"]
        if not re.fullmatch(r"[A-Z0-9]{6}", code):
            raise ValueError
        identifier = order_id(scope, row["ord_dt"], row["ord_gno_brno"], row["odno"])
        side = {"01": "sell", "02": "buy"}[row["sll_buy_dvsn_cd"]]
        kind = {"00": "limit", "01": "market"}[row["ord_dvsn_cd"]]
        amount, filled, remaining, canceled, rejected = [
            int(number(row[key], integer=True))
            for key in ("ord_qty", "tot_ccld_qty", "rmn_qty", "cnc_cfrm_qty", "rjct_qty")
        ]
        cost = number(row["tot_ccld_amt"], integer=True)
        if not amount or filled + remaining + canceled + rejected != amount:
            raise ValueError
        if bool(filled) != bool(cost):
            raise ValueError
        status = (
            "open"
            if remaining
            else "closed"
            if filled == amount
            else "rejected"
            if rejected == amount
            else "canceled"
        )
        price = float(number(row["ord_unpr"])) if kind == "limit" else None
        stamp = broker_timestamp(row["ord_dt"], row["ord_tmd"])
        return {
            "id": identifier,
            "symbol": code + "/KRW",
            "type": kind,
            "side": side,
            "timestamp": stamp,
            "datetime": datetime.fromtimestamp(stamp / 1000, UTC).isoformat(),
            "timeInForce": "DAY",
            "amount": amount,
            "filled": filled,
            "remaining": remaining,
            "status": status,
            "price": price,
            "average": float(cost / filled) if filled else None,
            "cost": float(cost),
            "fee": None,
            "trades": None,
            "info": {
                "businessDate": row["ord_dt"],
                "branch": row["ord_gno_brno"],
                "brokerOrderId": row["odno"],
                "canceledQuantity": canceled,
                "rejectedQuantity": rejected,
                "costStatus": "unverified",
                "brokerAverage": float(number(row["avg_prvs"])) if filled else None,
            },
        }
    except (KeyError, ValueError, TypeError):
        raise ccxt.ExchangeError("kis inconsistent or unsupported order snapshot") from None
