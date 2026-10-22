"""Network-isolated paper-account scenarios; all balances and orders are synthetic."""

from copy import deepcopy
from datetime import timedelta

import ccxt
import pytest

from freqtrade.enums import RunMode
from freqtrade.exchange.kis_adapter import KisAsync, KisSync
from freqtrade.exchange.kis_adapter.client import KisClient
from freqtrade.exchange.kis_adapter.trading import KisTrading
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


def import_order(order):
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
    import_order(order)
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
    import_order(order)
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
    import_order(buy)
    broker.fill(0, 10, 100000)
    service.fetch_orders()
    sell = service.create_order("005930/KRW", "limit", "sell", 10, 10000)
    import_order(sell)
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
