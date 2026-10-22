"""Explicit paper checks; run with python -m freqtrade.exchange.kis_adapter.validate."""

import argparse
import copy
import hashlib
import json
import logging
import os
import platform
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import ccxt

import freqtrade
from freqtrade.configuration import Configuration
from freqtrade.enums import RunMode
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Order, Trade
from freqtrade.util import dt_now


@contextmanager
def audit_requests(action, allow_orders, events):
    original = KisClient._http
    permitted = {
        "submit": "/uapi/domestic-stock/v1/trading/order-cash",
        "cancel": "/uapi/domestic-stock/v1/trading/order-rvsecncl",
    }.get(action)

    def request(client, method, path, **kwargs):
        endpoint = path.partition("?")[0]
        if client.environment != "paper" or (
            method != "GET"
            and endpoint != "/oauth2/tokenP"
            and not (method == "POST" and allow_orders and endpoint == permitted)
        ):
            raise ccxt.PermissionDenied("paper validation request blocked")
        events.append({"method": method, "path": endpoint})
        return original(client, method, path, **kwargs)

    with patch.object(KisClient, "_http", request):
        yield


def prepare_config(config):
    result = copy.deepcopy(config)
    exchange = result.get("exchange", {})
    if exchange.get("name") != "kis" or exchange.get("kis", {}).get("environment") != "paper":
        raise ValueError("validation requires an explicit KIS paper configuration")
    if not result.get("db_url") or not result.get("original_config", result).get("db_url"):
        raise ValueError("validation requires a dedicated database URL")
    result.update(
        dry_run=False,
        runmode=RunMode.LIVE,
        strategy="KisValidationStrategy",
        strategy_path=str(Path(__file__).parent),
        recursive_strategy_search=False,
        initial_state="stopped",
        cancel_open_orders_on_exit=False,
        position_adjustment_enable=False,
    )
    for channel in ("telegram", "discord", "webhook", "api_server", "external_message_consumer"):
        result.pop(channel, None)
    return result


def compare_database(bot):
    service = bot.exchange._api._trading
    bot.recover_exchange_order_intents()
    balance = service.fetch_balance()
    canonical = service.store.orders()
    rows, checks = [], []
    fields = ("status", "filled", "remaining", "cost", "average")
    for snapshot in canonical:
        local = Order.order_by_id(snapshot["id"], snapshot["symbol"])
        expected_empty = snapshot["status"] != "open" and not snapshot["filled"]
        matches = (local is None and expected_empty) or (
            local is not None and all(getattr(local, field) == snapshot[field] for field in fields)
        )
        checks.append({"name": "order_matches_db", "order": snapshot["id"], "passed": matches})
        rows.append(
            {key: snapshot[key] for key in ("id", "symbol", "side", "type", *fields, "amount")}
        )
    held = {}
    for trade in Trade.get_open_trades():
        code = trade.pair.removesuffix("/KRW")
        held[code] = held.get(code, 0) + trade.amount
    held = {code: amount for code, amount in held.items() if amount}
    broker = {
        code: wallet["total"]
        for code, wallet in balance.items()
        if code not in ("KRW", "info", "free", "used", "total") and wallet["total"]
    }
    checks.append({"name": "positions_match_db", "passed": held == broker})
    indexed = {order["id"]: order for order in canonical}
    for trade in Trade.get_trades_proxy():
        owned = [indexed[o.order_id] for o in trade.orders if o.order_id in indexed]
        for side, suffix in (("buy", "open"), ("sell", "close")):
            matching = [o for o in owned if o["side"] == side and o["filled"]]
            if matching:
                expected = sum(o["fee"]["cost"] for o in matching)
                checks.append(
                    {
                        "name": "estimated_cost_matches_db",
                        "trade": trade.id,
                        "side": side,
                        "passed": getattr(trade, "fee_" + suffix + "_cost") == expected,
                    }
                )
    checks.append({"name": "accounting_acknowledged", "passed": not service.store.unimported()})
    fills = service.store.trades()
    return {
        "checks": checks,
        "orders": rows,
        "synthetic_fill_count": len(fills),
        "cash": balance["KRW"],
        "positions": broker,
        "cost_status": "estimated",
    }


def validate(
    config,
    *,
    action="preflight",
    allow_orders=False,
    symbol=None,
    side=None,
    kind="limit",
    amount=1,
    price=None,
    order_id=None,
):
    report = {
        "environment": "paper",
        "action": action,
        "started_at": dt_now().isoformat(),
        "status": "failed",
        "phase6_complete": False,
        "versions": {
            "freqtrade": freqtrade.__version__,
            "ccxt": ccxt.__version__,
            "python": platform.python_version(),
        },
        "source_hashes": {
            str(path.relative_to(Path(freqtrade.__file__).parent)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in [
                *Path(__file__).parent.glob("*.py"),
                Path(freqtrade.__file__).parent / "exchange/kis.py",
                Path(freqtrade.__file__).parent / "freqtradebot.py",
                Path(freqtrade.__file__).parent / "persistence/trade_model.py",
            ]
        },
        "requests": [],
        "unverified": ["FreqUI comparison", "final order costs", "full paper scenario coverage"],
    }
    bot = None
    stage = "configuration"
    try:
        config = prepare_config(config)
        if action not in ("preflight", "submit", "cancel", "reconcile") or (
            action in ("submit", "cancel") and not allow_orders
        ):
            raise ValueError("unsupported action or missing --allow-paper-orders")
        symbol = symbol or config["exchange"]["pair_whitelist"][0]
        if symbol not in config["exchange"]["pair_whitelist"]:
            raise ValueError("validation requires a configured symbol")
        if action == "submit" and (
            side not in ("buy", "sell")
            or kind not in ("limit", "market")
            or type(amount) is not int
            or amount < 1
            or (kind == "limit" and price is None)
        ):
            raise ValueError("submission requires side, whole shares and a limit price")
        if action == "cancel" and not order_id:
            raise ValueError("cancellation requires an owned order ID")
        with audit_requests(action, allow_orders, report["requests"]):
            stage = "initialization"
            bot = FreqtradeBot(config)
            stage = "recovery"
            bot.recover_exchange_order_intents()
            stage = action
            if action == "preflight":
                ticker = bot.exchange._api.fetch_ticker(symbol)
                report["quote"] = {"symbol": symbol, "last": ticker["last"]}
            elif action == "submit":
                reference = price
                if reference is None:
                    book = bot.exchange._api.fetch_order_book(symbol, 1)
                    reference = book["asks" if side == "buy" else "bids"][0][0]
                order = bot.exchange.create_order(
                    pair=symbol,
                    ordertype=kind,
                    side=side,
                    amount=amount,
                    rate=reference,
                    leverage=1,
                    time_in_force="DAY",
                )
                report["submitted_order"] = order["id"]
            elif action == "cancel":
                bot.exchange.cancel_order(order_id, symbol)
            stage = "database_comparison"
            report.update(compare_database(bot))
            report["status"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = {"stage": stage, "type": type(exc).__name__}
    finally:
        if bot is not None:
            try:
                with audit_requests(action, allow_orders, report["requests"]):
                    bot.cleanup()
            except Exception as exc:
                report["status"] = "failed"
                report["error"] = {"stage": "cleanup", "type": type(exc).__name__}
        report["finished_at"] = dt_now().isoformat()
        report["broker_mutation_attempts"] = sum(
            event["method"] == "POST" and event["path"] != "/oauth2/tokenP"
            for event in report["requests"]
        )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--action", choices=("preflight", "submit", "cancel", "reconcile"), default="preflight"
    )
    parser.add_argument("--allow-paper-orders", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--side", choices=("buy", "sell"))
    parser.add_argument("--kind", choices=("limit", "market"), default="limit")
    parser.add_argument("--amount", type=int, default=1)
    parser.add_argument("--price", type=float)
    parser.add_argument("--order-id")
    args = parser.parse_args(argv)
    try:
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(2, "Report must be a new writable file. No broker request was sent.\n")
    with os.fdopen(descriptor, "w") as output:
        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            config = Configuration({"config": args.config}, RunMode.LIVE).get_config()
            report = validate(
                config,
                **{
                    key: getattr(args, key)
                    for key in ("action", "symbol", "side", "kind", "amount", "price", "order_id")
                },
                allow_orders=args.allow_paper_orders,
            )
        except (Exception, KeyboardInterrupt) as exc:
            report = {
                "status": "failed",
                "phase6_complete": False,
                "error": {"stage": "configuration", "type": type(exc).__name__},
            }
        finally:
            logging.disable(previous_logging)
        json.dump(report, output, indent=2)
        output.write("\n")
    print("Paper validation action " + report["status"] + "; phase 6 remains unverified.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
