"""Network-isolated paper-account scenarios; all balances and orders are synthetic."""

from copy import deepcopy
from datetime import timedelta

import ccxt
import pytest

from freqtrade.enums import RunMode
from freqtrade.exchange.kis_adapter import KisAsync, KisSync
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.trading import KisTrading
from freqtrade.exchange.kis_adapter.validate import audit_requests, main, validate
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Order, Trade, init_db
from freqtrade.util import dt_ts
from tests.exchange.kis_adapter.conftest import COST_RULE, NOW, SCHEDULE
from tests.exchange.kis_adapter.test_client import CONFIG, TOKEN, make_client
from tests.exchange.kis_adapter.test_orders import synthetic_order


class PaperBroker:
    def __init__(self):
        self.rows = []
        self.holdings = {}
        self.cash = 1000000
        self.cap = 1000000
        self.quantity = 100
        self.calls = []
        self.timeout = False
        self.cancel_immediate = True
        self.service = None

    def pages(self, path, tr, params):
        self.calls.append((path, tr, params))
        if path.endswith("inquire-daily-ccld"):
            return [
                {
                    "output1": deepcopy(
                        [
                            r
                            for r in self.rows
                            if params["INQR_STRT_DT"] <= r["ord_dt"] <= params["INQR_END_DT"]
                        ]
                    )
                }
            ]
        if path.endswith("inquire-psbl-rvsecncl"):
            return [
                {
                    "output": [
                        {
                            "odno": r["odno"],
                            "ord_gno_brno": r["ord_gno_brno"],
                            "psbl_qty": r["rmn_qty"],
                        }
                        for r in self.rows
                        if int(r["rmn_qty"])
                    ]
                }
            ]
        assert path.endswith("inquire-balance")
        return [
            {
                "output1": [
                    {"pdno": code, "hldg_qty": str(qty), "ord_psbl_qty": str(qty)}
                    for code, qty in self.holdings.items()
                ],
                "output2": [
                    {
                        "dnca_tot_amt": str(self.cash),
                        "nxdy_excc_amt": str(self.cash),
                        "prvs_rcdl_excc_amt": str(self.cash),
                        "tot_loan_amt": "0",
                    }
                ],
            }
        ]

    def get(self, path, tr, params, continuation=""):
        assert path.endswith("inquire-psbl-order")
        assert params["ORD_DVSN"] == "01"
        assert params["CMA_EVLU_AMT_ICLD_YN"] == params["OVRS_ICLD_YN"] == "N"
        return {"output": {"nrcvb_buy_amt": str(self.cap), "nrcvb_buy_qty": str(self.quantity)}}, ""

    def post(self, path, tr, body):
        self.calls.append((path, tr, body))
        pending = [i for i in self.service.store.intents() if i["state"] == "unknown"]
        assert len(pending) == 1
        assert pending[0]["kind"] == ("create" if path.endswith("order-cash") else "cancel")
        if path.endswith("order-cash"):
            row = synthetic_order(
                odno=str(1001 + len(self.rows)),
                pdno=body["PDNO"],
                sll_buy_dvsn_cd="02" if tr.endswith("12U") else "01",
                ord_dvsn_cd=body["ORD_DVSN"],
                ord_qty=body["ORD_QTY"],
                rmn_qty=body["ORD_QTY"],
                ord_unpr=body["ORD_UNPR"],
                ord_tmd=NOW.strftime("%H%M%S"),
            )
            self.rows.append(row)
            output = {
                "ODNO": row["odno"],
                "KRX_FWDG_ORD_ORGNO": row["ord_gno_brno"],
                "ORD_TMD": row["ord_tmd"],
            }
        else:
            assert body["RVSE_CNCL_DVSN_CD"] == "02" and body["QTY_ALL_ORD_YN"] == "Y"
            row = next(r for r in self.rows if r["odno"] == body["ORGN_ODNO"])
            if self.cancel_immediate:
                row["cnc_cfrm_qty"] = str(int(row["cnc_cfrm_qty"]) + int(row["rmn_qty"]))
                row["rmn_qty"] = "0"
            output = {"ODNO": "cancel-ack"}
        if self.timeout:
            raise ccxt.RequestTimeout("synthetic lost response")
        return {"rt_cd": "0", "output": output}

    def fill(self, index, quantity, cost):
        row = self.rows[index]
        row.update(
            tot_ccld_qty=str(quantity),
            tot_ccld_amt=str(cost),
            avg_prvs=str(cost / quantity),
            rmn_qty=str(int(row["ord_qty"]) - quantity - int(row["cnc_cfrm_qty"])),
        )
        signed = 1 if row["sll_buy_dvsn_cd"] == "02" else -1
        self.holdings[row["pdno"]] = self.holdings.get(row["pdno"], 0) + signed * quantity


@pytest.fixture
def trading(tmp_path, mocker):
    url = "sqlite:///" + str(tmp_path / "bot.sqlite")
    init_db(url)
    options = {
        "environment": "paper",
        "enablePaperOrders": True,
        "dbUrl": url,
        "symbols": ["005930/KRW"],
        "sessions": {"2026-10-22": SCHEDULE},
        "costs": {"005930/KRW": [COST_RULE]},
        "settlementCalendar": {
            (NOW.date() + timedelta(days=i)).isoformat(): {
                "settles": i in (1, 4),
                "source": "synthetic settlement calendar",
            }
            for i in range(1, 5)
        },
    }
    client = KisClient(CONFIG | {"options": options})
    broker = PaperBroker()
    service = KisTrading(client, options)
    client._trading = service
    broker.service = service
    mocker.patch.object(client, "pages", side_effect=broker.pages)
    mocker.patch.object(client, "get", side_effect=broker.get)
    mocker.patch.object(client, "post", side_effect=broker.post)
    mocker.patch("freqtrade.exchange.kis_adapter.trading.dt_now", return_value=NOW)
    mocker.patch(
        "freqtrade.exchange.kis_adapter.trading.dt_ts",
        side_effect=lambda value=None: dt_ts(value or NOW),
    )
    yield service, broker
    service.close()


def import_order(order, service):
    trade = Trade(
        pair=order["symbol"],
        exchange="kis",
        open_rate=10000,
        stake_amount=10000,
        amount=0,
        fee_open=0.001,
        fee_close=0.003,
    )
    trade.orders.append(Order.parse_from_ccxt_object(order, order["symbol"], order["side"]))
    Trade.session.add(trade)
    Trade.commit()
    service.store.mark_imported(order)


def test_clean_cash_and_working_reservation_are_not_settlement_balances(trading):
    service, broker = trading
    assert service.fetch_balance()["KRW"] == {"free": 1000000, "used": 0, "total": 1000000}
    order = service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    assert order["status"] == "open" and order["filled"] == 0
    wallet = service.fetch_balance()
    assert wallet["KRW"] == {"free": 899900, "used": 100100, "total": 1000000}
    assert wallet["info"]["brokerSettlement"]["dnca_tot_amt"] == 1000000
    broker.fill(0, 3, 30000)
    wallet = service.fetch_balance()
    assert wallet["KRW"]["total"] == 969970
    assert wallet["info"]["reserved"] == 70070
    assert wallet["005930"] == {"free": 3, "used": 0, "total": 3}
    fills = service.fetch_my_trades()
    assert sum(t["amount"] for t in fills) == 3
    assert len(service.fetch_my_trades()) == 1


def test_lost_submission_is_recovered_and_never_resent_before_bot_import(trading):
    service, broker = trading
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    assert service.store.intents()[0]["state"] == "unknown"
    broker.timeout = False
    with pytest.raises(ccxt.ExchangeError, match="awaiting bot DB import"):
        service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    assert len([c for c in broker.calls if c[0].endswith("order-cash")]) == 1
    assert service.store.intents()[0]["state"] == "accepted"
    recovered = service.store.unimported()
    assert len(recovered) == 1 and recovered[0]["filled"] == 0


def test_missing_history_does_not_resend_an_unresolved_request(trading):
    service, broker = trading
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    broker.rows.clear()
    with pytest.raises(ccxt.ExchangeError, match="never resend"):
        service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    assert len([c for c in broker.calls if c[0].endswith("order-cash")]) == 1


def test_unknown_intent_and_fill_watermark_survive_service_restart(trading):
    service, broker = trading
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.fill(0, 3, 30000)
    service.close()
    restored = KisTrading(service.client, service.options)
    service.client._trading = restored
    broker.service = restored
    try:
        assert restored.fetch_balance()["005930"]["total"] == 3
        first = restored.fetch_my_trades()
        assert len(first) == 1 and first[0]["amount"] == 3
        assert restored.fetch_my_trades() == first
        assert len([c for c in broker.calls if c[0].endswith("order-cash")]) == 1
    finally:
        restored.close()


def test_closed_session_blocks_mutations_but_keeps_owned_history_available(trading, mocker):
    service, broker = trading
    order = service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    mocker.patch(
        "freqtrade.exchange.kis_adapter.trading.dt_now", return_value=NOW + timedelta(hours=8)
    )
    assert service.fetch_order(order["id"])["status"] == "open"
    assert service.fetch_balance()["KRW"]["total"] == 1000000
    with pytest.raises(ccxt.InvalidOrder, match="fresh verified open session"):
        service.cancel_order(order["id"])
    assert not any(c[0].endswith("order-rvsecncl") for c in broker.calls)


@pytest.mark.parametrize("output", [[None], [{"odno": "1001"}], []])
def test_incomplete_cancel_eligibility_never_creates_a_mutation(trading, output):
    service, broker = trading
    order = service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    original = broker.pages

    def pages(path, tr, params):
        if path.endswith("inquire-psbl-rvsecncl"):
            return [{"output": output}]
        return original(path, tr, params)

    service.client.pages.side_effect = pages
    with pytest.raises(ccxt.ExchangeError, match="cancellation eligibility"):
        service.cancel_order(order["id"])
    assert len(service.store.intents()) == 1
    assert not any(c[0].endswith("order-rvsecncl") for c in broker.calls)


def test_delayed_history_after_partial_fill_recovers_without_a_false_external_block(trading):
    service, broker = trading
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.timeout = False
    broker.fill(0, 3, 30000)
    rows, broker.rows = broker.rows, []
    with pytest.raises(ccxt.ExchangeError, match="not yet visible"):
        service.fetch_balance()
    service.store.assert_consistent()
    broker.rows = rows
    wallet = service.fetch_balance()
    assert wallet["005930"]["total"] == 3
    assert len(service.fetch_my_trades()) == 1
    assert service.store.intents()[0]["state"] == "accepted"
    assert len([c for c in broker.calls if c[0].endswith("order-cash")]) == 1


def test_ambiguous_recovery_blocks_instead_of_claiming_an_order(trading):
    service, broker = trading
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    broker.rows.append(broker.rows[0] | {"odno": "1002"})
    with pytest.raises(ccxt.ExchangeError, match="uniquely reconcile"):
        service.fetch_orders()
    assert service.store.intents()[0]["state"] == "unknown"


def test_cash_transfer_blocks_orders_but_owned_order_queries_continue(trading):
    service, broker = trading
    order = service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    broker.cash += 100
    with pytest.raises(ccxt.ExchangeError, match="cash or positions"):
        service.fetch_balance()
    assert service.fetch_order(order["id"])["status"] == "open"
    assert service.fetch_my_trades() == []
    with pytest.raises(ccxt.ExchangeError):
        service.create_order("005930/KRW", "limit", "buy", 1, 10000)


def test_cancel_ack_is_not_terminal_and_late_fills_are_retained(trading):
    service, broker = trading
    order = service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    import_order(order, service)
    broker.fill(0, 3, 30000)
    broker.cancel_immediate = False
    pending = service.cancel_order(order["id"])
    assert pending["status"] == "open" and pending["filled"] == 3
    with pytest.raises(ccxt.ExchangeError, match="pending"):
        service.cancel_order(order["id"])
    broker.rows[0].update(
        tot_ccld_qty="4", tot_ccld_amt="41000", avg_prvs="10250", rmn_qty="0", cnc_cfrm_qty="6"
    )
    broker.holdings["005930"] = 4
    terminal = service.fetch_order(order["id"])
    assert terminal["status"] == "canceled" and terminal["filled"] == 4
    assert sum(t["cost"] for t in service.fetch_my_trades()) == 41000
    assert len([c for c in broker.calls if c[0].endswith("order-rvsecncl")]) == 1


def test_lost_cancel_response_is_reconciled_without_another_cancel(trading):
    service, broker = trading
    order = service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    import_order(order, service)
    broker.fill(0, 3, 30000)
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.cancel_order(order["id"])
    result = service.fetch_order(order["id"])
    assert result["status"] == "canceled" and result["filled"] == 3
    assert all(i["state"] == "resolved" for i in service.store.intents())
    assert service.cancel_order(order["id"])["status"] == "canceled"
    assert len([c for c in broker.calls if c[0].endswith("order-rvsecncl")]) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_sync_and_async_private_contracts_agree(trading, sources, asynchronous):
    service, broker = trading
    config = CONFIG | {"options": service.options}
    api = (KisAsync if asynchronous else KisSync)(config, client=service.client)

    async def invoke(name, *args, **kwargs):
        result = getattr(api, name)(*args, **kwargs)
        return await result if asynchronous else result

    try:
        assert api.has["createOrder"] and api.has["fetchMyTrades"]
        assert (await invoke("fetch_balance"))["KRW"]["free"] == 1000000
        order = await invoke(
            "create_order",
            "005930/KRW",
            type="limit",
            side="buy",
            amount=10,
            price=10000,
            params={"timeInForce": "DAY"},
        )
        broker.fill(0, 3, 30000)
        partial = await invoke("fetch_order", id=order["id"])
        assert partial["filled"] == 3 and partial["remaining"] == 7 and partial["status"] == "open"
        opened = await invoke("fetch_open_orders", symbol="005930/KRW", limit=1)
        assert [o["id"] for o in opened] == [order["id"]]
        fills = await invoke(
            "fetch_my_trades",
            symbol="005930/KRW",
            since=dt_ts(NOW),
            limit=1,
            params={"order": order["id"]},
        )
        assert len(fills) == 1 and fills[0]["amount"] == 3 and fills[0]["cost"] == 30000
        assert fills[0]["info"]["timestamp_kind"] == "observation"
        canceled = await invoke("cancel_order", id=order["id"])
        assert canceled["filled"] == 3 and canceled["status"] == "canceled"
    finally:
        if asynchronous:
            await api.close()
        else:
            api.close()


async def test_failed_async_option_binding_releases_its_acquired_reference():
    synchronous = KisSync(CONFIG)
    try:
        with pytest.raises(ccxt.AuthenticationError, match="options mismatch"):
            KisAsync(CONFIG | {"options": {"sessions": {}}}, client=synchronous._client)
        assert synchronous._client._owners == 1
    finally:
        synchronous.close()
    assert synchronous._client._closed


@pytest.mark.parametrize("amount", [1.5, 0, -1])
def test_invalid_quantity_never_reaches_a_mutation(trading, amount):
    service, broker = trading
    with pytest.raises(ccxt.BaseError):
        service.create_order("005930/KRW", "limit", "buy", amount, 10000)
    assert not broker.calls


def test_insufficient_cash_and_sellable_quantity_never_mutate(trading):
    service, broker = trading
    broker.cap = 5000
    with pytest.raises(ccxt.InsufficientFunds):
        service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    with pytest.raises(ccxt.InsufficientFunds):
        service.create_order("005930/KRW", "market", "sell", 1, None)
    assert all("order-cash" not in c[0] for c in broker.calls)


@pytest.fixture
def paper_bot(trading, kis_config, sources, mocker):
    service, broker = trading
    kis_config.update(
        dry_run=False,
        runmode=RunMode.LIVE,
        db_url=service.options["dbUrl"],
        stake_amount=100000,
        dry_run_wallet=1000000,
    )
    kis_config["exchange"].update(account_id=CONFIG["accountId"], uid=CONFIG["uid"])
    kis_config["exchange"]["kis"]["settlement_calendar"] = service.options["settlementCalendar"]
    mocker.patch.object(KisTrading, "bind", return_value=service)
    mocker.patch("freqtrade.freqtradebot.RPCManager")
    bot = FreqtradeBot(kis_config)
    try:
        yield bot, service, broker
    finally:
        bot.cleanup()


@pytest.mark.parametrize("filled", [0, 3])
def test_engine_imports_a_lost_entry_once_and_preserves_partial_fills(paper_bot, filled):
    bot, service, broker = paper_bot
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.timeout = False
    if filled:
        broker.fill(0, filled, 31001)
    bot.recover_exchange_order_intents()
    bot.recover_exchange_order_intents()
    trades = Trade.get_open_trades()
    assert len(trades) == 1 and len(trades[0].orders) == 1
    trade = trades[0]
    assert trade.amount == filled
    assert trade.orders[0].filled == filled
    assert trade.orders[0].remaining == 10 - filled
    assert trade.open_date_utc == NOW.replace(microsecond=0).astimezone(NOW.tzinfo)
    if filled:
        assert trade.open_rate == 31001 / 3
        assert trade.stake_amount == 31001
        assert trade.fee_open_cost == 32
    assert service.store.unimported() == []
    assert len([c for c in broker.calls if c[0].endswith("order-cash")]) == 1


def test_terminal_zero_fill_entry_has_history_without_an_open_trade(paper_bot):
    bot, service, broker = paper_bot
    broker.timeout = True
    with pytest.raises(ccxt.RequestTimeout):
        service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.rows[0].update(rmn_qty="0", cnc_cfrm_qty="10")
    bot.recover_exchange_order_intents()
    bot.recover_exchange_order_intents()
    assert Trade.get_open_trades() == []
    assert service.store.orders()[0]["status"] == "canceled"
    assert service.store.intents()[0]["state"] == "resolved"
    assert service.store.unimported() == []


def test_normally_canceled_empty_entry_is_not_resurrected(paper_bot):
    bot, service, broker = paper_bot
    order = service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    bot.recover_exchange_order_intents()
    trade = Trade.get_open_trades()[0]
    broker.rows[0].update(rmn_qty="0", cnc_cfrm_qty="10")
    canceled = service.fetch_order(order["id"])
    assert bot.update_trade_state(trade, order["id"], canceled, send_msg=False)
    bot.handle_cancel_enter(trade, canceled, trade.orders[0], "synthetic cancellation")
    assert Trade.get_open_trades() == []
    bot.recover_exchange_order_intents()
    assert Trade.get_open_trades() == []
    assert service.store.orders()[0]["status"] == "canceled"


@pytest.mark.parametrize("boundary", ["before_accounting", "before_acknowledgement"])
def test_interrupted_terminal_exit_import_replays_accounting(paper_bot, mocker, boundary):
    bot, service, broker = paper_bot
    service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.fill(0, 10, 100000)
    bot.recover_exchange_order_intents()
    trade = Trade.get_open_trades()[0]
    sell = service.create_order("005930/KRW", "limit", "sell", 10, 11000)
    broker.fill(1, 10, 110000)
    target, name = (
        (bot, "update_trade_state")
        if boundary == "before_accounting"
        else (bot.exchange, "acknowledge_order_intent")
    )
    interrupted = mocker.patch.object(
        target, name, side_effect=RuntimeError("synthetic interruption")
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        bot.recover_exchange_order_intents()
    assert Order.order_by_id(sell["id"]) is not None
    assert service.store.unimported()[0]["id"] == sell["id"]
    mocker.stop(interrupted)
    Trade.session.expire_all()
    bot.recover_exchange_order_intents()
    bot.recover_exchange_order_intents()
    Trade.session.refresh(trade)
    assert not trade.is_open and trade.close_profit_abs == 9570
    assert service.store.unimported() == []
    assert len(service.fetch_my_trades()) == 2
    assert len([c for c in broker.calls if c[0].endswith("order-cash")]) == 2


def test_entry_canceled_during_downtime_is_cleaned_before_startup_refresh(paper_bot):
    bot, service, broker = paper_bot
    service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    bot.recover_exchange_order_intents()
    assert service.store.intents()[0]["imported"] == 1
    broker.rows[0].update(rmn_qty="0", cnc_cfrm_qty="10")
    Trade.session.expire_all()
    bot.recover_exchange_order_intents()
    bot.startup_update_open_orders()
    bot.manage_open_orders()
    bot.recover_exchange_order_intents()
    assert Trade.get_open_trades() == []
    assert service.store.unimported() == []
    assert not any(c[0].endswith("order-rvsecncl") for c in broker.calls)


def test_later_terminal_fill_replays_after_order_update_commit_interruption(paper_bot, mocker):
    bot, service, broker = paper_bot
    service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.fill(0, 10, 100000)
    bot.recover_exchange_order_intents()
    trade = Trade.get_open_trades()[0]
    sell = service.create_order("005930/KRW", "limit", "sell", 10, 11000)
    bot.recover_exchange_order_intents()
    assert all(i["imported"] for i in service.store.intents())
    broker.fill(1, 10, 110000)
    interrupted = mocker.patch.object(
        bot.exchange, "update_trade_from_order", side_effect=RuntimeError("synthetic interruption")
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        bot.update_trade_state(trade, sell["id"], service.fetch_order(sell["id"]), send_msg=False)
    assert Order.order_by_id(sell["id"]).status == "closed"
    assert next(i for i in service.store.intents() if i["order_id"] == sell["id"])["imported"] == 0
    mocker.stop(interrupted)
    Trade.session.expire_all()
    bot.recover_exchange_order_intents()
    bot.startup_update_open_orders()
    bot.recover_exchange_order_intents()
    Trade.session.refresh(trade)
    assert not trade.is_open and trade.close_profit_abs == 9570
    assert service.store.unimported() == []
    assert len(service.fetch_my_trades()) == 2


def test_new_fill_during_wallet_refresh_cannot_acknowledge_an_older_snapshot(paper_bot, mocker):
    bot, service, broker = paper_bot
    service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.fill(0, 10, 100000)
    bot.recover_exchange_order_intents()
    trade = Trade.get_open_trades()[0]
    sell = service.create_order("005930/KRW", "limit", "sell", 10, 11000)
    bot.recover_exchange_order_intents()
    broker.fill(1, 3, 33000)
    first = service.fetch_order(sell["id"])

    def refresh(*args, **kwargs):
        broker.rows[1].update(
            tot_ccld_qty="10", tot_ccld_amt="110000", avg_prvs="11000", rmn_qty="0"
        )
        broker.holdings["005930"] = 0
        service.fetch_balance()

    mocker.patch.object(bot.wallets, "update", side_effect=refresh)
    bot.update_trade_state(trade, sell["id"], first, send_msg=False)
    assert trade.amount == 7
    assert next(i for i in service.store.intents() if i["order_id"] == sell["id"])["imported"] == 0
    bot.recover_exchange_order_intents()
    assert not trade.is_open and trade.close_profit_abs == 9570
    assert service.store.unimported() == []


def test_partial_exit_cumulative_updates_do_not_close_early_or_double_charge(paper_bot):
    bot, service, broker = paper_bot
    service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.fill(0, 10, 100000)
    bot.recover_exchange_order_intents()
    trade = Trade.get_open_trades()[0]
    assert trade.amount == 10
    sell = service.create_order("005930/KRW", "limit", "sell", 10, 11000)
    bot.recover_exchange_order_intents()
    for quantity in (3, 5, 7, 10):
        broker.rows[1].update(
            tot_ccld_qty=str(quantity),
            tot_ccld_amt=str(quantity * 11000),
            avg_prvs="11000",
            rmn_qty=str(10 - quantity),
        )
        broker.holdings["005930"] = 10 - quantity
        snapshot = service.fetch_order(sell["id"])
        bot.update_trade_state(trade, sell["id"], snapshot, send_msg=False)
        bot.update_trade_state(trade, sell["id"], snapshot, send_msg=False)
        if quantity < 10:
            assert trade.is_open and trade.amount == 10 - quantity
        else:
            assert not trade.is_open
    assert trade.fee_open_cost == 100
    assert trade.fee_close_cost == 330
    assert trade.close_profit_abs == 9570
    assert sum(t["amount"] for t in service.fetch_my_trades(order=sell["id"])) == 10
    wallet = service.fetch_balance()
    assert wallet["KRW"]["total"] == 1009570
    assert wallet["info"]["unsettledSales"] == 109670
    assert wallet["KRW"]["free"] == 899900


def test_unsettled_sale_cash_is_held_until_calendar_and_broker_agree(trading, mocker):
    service, broker = trading
    buy = service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.fill(0, 10, 100000)
    import_order(service.fetch_order(buy["id"]), service)
    sell = service.create_order("005930/KRW", "limit", "sell", 10, 10000)
    import_order(sell, service)
    broker.fill(1, 10, 100000)
    wallet = service.fetch_balance()
    assert wallet["KRW"] == {"total": 999600, "free": 899900, "used": 99700}
    monday = NOW + timedelta(days=4)
    mocker.patch("freqtrade.exchange.kis_adapter.trading.dt_now", return_value=monday)
    mocker.patch(
        "freqtrade.exchange.kis_adapter.trading.dt_ts",
        side_effect=lambda value=None: dt_ts(value or monday),
    )
    broker.cash = 999600
    settled = service.fetch_balance()
    assert settled["KRW"] == {"total": 999600, "free": 999600, "used": 0}


def test_account_changes_during_snapshot_require_a_fresh_stable_read(trading):
    service, broker = trading
    original = broker.pages

    def pages(path, tr, params):
        if path.endswith("inquire-balance"):
            broker.cash += 1
        return original(path, tr, params)

    service.client.pages.side_effect = pages
    with pytest.raises(ccxt.ExchangeError, match="changed during reconciliation"):
        service.fetch_balance()
    assert service.store.baseline() is None


def test_old_day_working_order_is_queried_but_not_canceled_by_clock(trading, mocker):
    service, broker = trading
    order = service.create_order("005930/KRW", "limit", "buy", 1, 10000)
    tomorrow = NOW + timedelta(days=1)
    record = SCHEDULE | {"verified_at": tomorrow.isoformat()}
    service.options["sessions"][tomorrow.date().isoformat()] = record
    service.data._sessions[tomorrow.date().isoformat()] = record
    mocker.patch("freqtrade.exchange.kis_adapter.trading.dt_now", return_value=tomorrow)
    assert service.fetch_order(order["id"])["status"] == "open"
    with pytest.raises(ccxt.InvalidOrder, match="prior-day"):
        service.cancel_order(order["id"])
    assert not any(c[0].endswith("order-rvsecncl") for c in broker.calls)


def test_paper_post_is_single_attempt_and_production_writes_are_rejected(clock):
    config = CONFIG | {"options": {"environment": "paper", "enablePaperOrders": True}}
    api, transport = make_client([TOKEN, ccxt.RequestTimeout("synthetic timeout")], config)
    body = {"CANO": "12345678", "ACNT_PRDT_CD": "01", "EXCG_ID_DVSN_CD": "KRX"}
    with pytest.raises(ccxt.RequestTimeout):
        api.post("trading/order-cash", "VTTC0012U", body)
    assert len(transport.calls) == 2
    assert transport.calls[-1].method == "POST"
    with pytest.raises(ccxt.PermissionDenied):
        KisClient(CONFIG | {"options": {"environment": "live", "enablePaperOrders": True}})
    with pytest.raises(ccxt.PermissionDenied):
        api.post("trading/order-cash", "TTTC0012U", body)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("action", ["submit", "cancel"])
def test_validation_mutations_require_explicit_opt_in_before_bot_init(kis_config, mocker, action):
    constructor = mocker.patch("freqtrade.exchange.kis_adapter.validate.FreqtradeBot")
    report = validate(kis_config, action=action)
    assert report["status"] == "failed" and report["error"]["stage"] == "configuration"
    assert report["broker_mutation_attempts"] == 0
    assert not report["phase6_complete"]
    constructor.assert_not_called()


@pytest.fixture
def validation_bot(paper_bot, mocker):
    bot, _service, _broker = paper_bot
    bot.config.setdefault("original_config", {})["db_url"] = bot.config["db_url"]
    factory = mocker.patch("freqtrade.exchange.kis_adapter.validate.FreqtradeBot", return_value=bot)
    cleanup = mocker.patch.object(bot, "cleanup")
    yield paper_bot, factory
    mocker.stop(cleanup)


def test_validation_reports_real_engine_and_db_comparison(validation_bot):
    (bot, _service, broker), factory = validation_bot
    config = deepcopy(bot.config)
    report = validate(
        config, action="submit", allow_orders=True, side="buy", amount=10, price=10000
    )
    assert report["status"] == "passed", report
    assert report["orders"][0]["filled"] == 0
    assert all(c["passed"] for c in report["checks"])
    broker.fill(0, 3, 30000)
    report = validate(config, action="reconcile")
    assert report["status"] == "passed", report
    assert report["positions"] == {"005930": 3}
    assert report["orders"][0]["filled"] == 3
    assert report["synthetic_fill_count"] == 1
    assert not report["phase6_complete"]
    supplied = factory.call_args.args[0]
    assert supplied["strategy"] == "KisValidationStrategy"
    assert not supplied["cancel_open_orders_on_exit"] and "api_server" not in supplied


def test_validation_mismatch_fails_without_inventing_success(validation_bot):
    (bot, service, broker), _factory = validation_bot
    service.create_order("005930/KRW", "limit", "buy", 10, 10000)
    broker.fill(0, 3, 30000)
    bot.recover_exchange_order_intents()
    trade = Trade.get_open_trades()[0]
    trade.amount = 2
    Trade.commit()
    report = validate(bot.config, action="reconcile")
    assert report["status"] == "failed"
    assert {"name": "positions_match_db", "passed": False} in report["checks"]


def test_validation_second_entry_is_rejected_before_broker_submission(validation_bot):
    (bot, _service, broker), _factory = validation_bot
    config = deepcopy(bot.config)
    assert (
        validate(config, action="submit", allow_orders=True, side="buy", price=10000)["status"]
        == "passed"
    )
    broker.fill(0, 1, 10000)
    assert validate(config, action="reconcile")["status"] == "passed"
    report = validate(config, action="submit", allow_orders=True, side="buy", price=10000)
    assert report["error"] == {"stage": "submit", "type": "InvalidOrder"}
    assert len(broker.rows) == 1


def test_validation_extra_db_order_fails_reconciliation_and_blocks_submission(validation_bot):
    (bot, service, broker), _factory = validation_bot
    config = deepcopy(bot.config)
    first = validate(config, action="submit", allow_orders=True, side="buy", price=10000)
    assert first["status"] == "passed"
    trade = Trade.get_open_trades()[0]
    unknown = service.store.orders()[0] | {"id": "synthetic-unknown-order"}
    trade.orders.append(Order.parse_from_ccxt_object(unknown, trade.pair, "buy"))
    Trade.commit()
    report = validate(config, action="reconcile")
    assert report["status"] == "failed"
    assert {"name": "db_orders_have_ledger", "passed": False} in report["checks"]
    report = validate(config, action="submit", allow_orders=True, side="buy", price=10000)
    assert report["error"] == {"stage": "before_mutation", "type": "ExchangeError"}
    assert len(broker.rows) == 1


def test_validation_initializes_fixed_strategy_and_cleans_up_without_canceling(
    trading, sources, kis_config, mocker
):
    service, broker = trading
    kis_config.update(db_url=service.options["dbUrl"], cancel_open_orders_on_exit=True)
    kis_config.setdefault("original_config", {})["db_url"] = kis_config["db_url"]
    kis_config["exchange"].update(account_id=CONFIG["accountId"], uid=CONFIG["uid"])
    kis_config["exchange"]["kis"]["settlement_calendar"] = service.options["settlementCalendar"]
    mocker.patch.object(KisTrading, "bind", return_value=service)
    report = validate(kis_config)
    assert report["status"] == "passed", report
    assert all(c["passed"] for c in report["checks"])
    assert report["quote"]["symbol"] == "005930/KRW"
    assert report["broker_mutation_attempts"] == 0 and broker.rows == []


def test_validation_transport_audit_blocks_writes_and_never_records_secrets(clock):
    config = CONFIG | {"options": {"environment": "paper", "enablePaperOrders": True}}
    events = []
    with audit_requests("preflight", False, events):
        api, transport = make_client([TOKEN], config)
        with pytest.raises(ccxt.PermissionDenied, match="blocked"):
            api.post(
                "trading/order-cash",
                "VTTC0012U",
                {
                    "CANO": CONFIG["accountId"],
                    "ACNT_PRDT_CD": CONFIG["uid"],
                    "EXCG_ID_DVSN_CD": "KRX",
                },
            )
    assert len(transport.calls) == 1
    assert events == [{"method": "POST", "path": "/oauth2/tokenP"}]
    assert all(secret not in str(events) for secret in CONFIG.values())


def test_validation_rejects_live_config_and_keeps_exception_text_out_of_report(kis_config, mocker):
    kis_config["exchange"]["kis"]["environment"] = "live"
    constructor = mocker.patch("freqtrade.exchange.kis_adapter.validate.FreqtradeBot")
    report = validate(kis_config)
    constructor.assert_not_called()
    assert report["status"] == "failed" and report["requests"] == []
    kis_config["exchange"]["kis"]["environment"] = "paper"
    kis_config["db_url"] = "sqlite:///synthetic.sqlite"
    kis_config.setdefault("original_config", {})["db_url"] = kis_config["db_url"]
    constructor.side_effect = RuntimeError("test-key test-secret private account values")
    report = validate(kis_config)
    assert report["error"] == {"stage": "initialization", "type": "RuntimeError"}
    assert "test-secret" not in str(report) and "private account values" not in str(report)


def test_validation_reports_are_private_and_never_overwritten(tmp_path, mocker):
    report = tmp_path / "report.json"
    loader = mocker.patch("freqtrade.exchange.kis_adapter.validate.Configuration")
    loader.side_effect = ValueError("private value")
    args = ["--config", "synthetic.json", "--report", str(report)]
    assert main(args) == 1
    assert report.stat().st_mode & 0o777 == 0o600
    assert "private value" not in report.read_text()
    original = report.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2 and report.read_bytes() == original
