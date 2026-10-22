"""Explicit, dated commission and gross-sale tax estimates."""

from datetime import date, datetime
from decimal import ROUND_CEILING

import ccxt

from freqtrade.exchange.kis_adapter.rules import SEOUL, number


def estimate_cost(
    profiles: dict, symbol: str, side: str, amount: float, price: float, when: datetime
) -> dict:
    """Estimate whole-KRW costs from exactly one sourced, effective symbol rule.

    This model covers ad-valorem commissions and gross-sale transaction taxes.
    It cannot estimate profit/NAV-dependent withholding or corporate actions.
    """
    try:
        day = when.astimezone(SEOUL).date()
        rules = [
            r
            for r in profiles[symbol]
            if date.fromisoformat(r["from"]) <= day <= date.fromisoformat(r["through"])
        ]
        if len(rules) != 1 or side not in ("buy", "sell") or when.tzinfo is None:
            raise ValueError
        rule = rules[0]
        if not isinstance(rule["source"], str) or not rule["source"].strip():
            raise ValueError
        if rule["tax_treatment"] not in ("stock_transaction_tax", "domestic_equity_etf"):
            raise ccxt.NotSupported("kis cost model cannot estimate this tax treatment")
        buy, sell, tax = [
            number(rule[key]) for key in ("buy_commission", "sell_commission", "sell_tax")
        ]
        if max(buy, sell, tax) > 0.1 or (rule["tax_treatment"] == "domestic_equity_etf" and tax):
            raise ValueError
        gross = number(amount, positive=True) * number(price, positive=True)
        commission = (gross * (buy if side == "buy" else sell)).to_integral_value(
            rounding=ROUND_CEILING
        )
        tax_cost = (gross * tax).to_integral_value(rounding=ROUND_CEILING) if side == "sell" else 0
        cost = commission + tax_cost
        return {
            "currency": "KRW",
            "cost": float(cost),
            "rate": float(cost / gross),
            "nominalRate": float(buy if side == "buy" else sell + tax),
            "estimated": True,
            "commission": float(commission),
            "tax": float(tax_cost),
            "source": rule["source"],
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ccxt.BadRequest(
            "kis requires one complete effective cost rule for each symbol"
        ) from None
