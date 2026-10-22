"""Synthetic order totals follow the pinned KIS daily-ccld field contract.

Source: examples_llm/domestic_stock/inquire_daily_ccld at
koreainvestment/open-trading-api@2077773a69a428daf63e148182511f068cc45de3.
These are constructed scenarios, not captured account responses.
"""

from copy import deepcopy

import ccxt
import pytest

from freqtrade.exchange.kis_adapter.orders import parse_order
from freqtrade.exchange.kis_adapter.store import KisStore
from freqtrade.persistence import init_db


def synthetic_order(**changes):
    row = {
        "pdno": "005930",
        "ord_dt": "20261022",
        "ord_gno_brno": "12345",
        "odno": "1001",
        "sll_buy_dvsn_cd": "02",
        "ord_dvsn_cd": "00",
        "ord_qty": "10",
        "ord_unpr": "10000",
        "ord_tmd": "090100",
        "tot_ccld_qty": "0",
        "tot_ccld_amt": "0",
        "avg_prvs": "0",
        "rmn_qty": "10",
        "cnc_cfrm_qty": "0",
        "rjct_qty": "0",
    }
    return row | changes


@pytest.fixture
def store(tmp_path):
    url = "sqlite:///" + str(tmp_path / "bot.sqlite")
    init_db(url)
    instance = KisStore(url, "test-account-scope")
    instance.seed(1000000, "2026-10-22")
    yield instance
    instance.close()


@pytest.mark.parametrize(
    "fill,cancel,reject,remaining,status",
    [
        (3, 2, 0, 5, "open"),
        (3, 7, 0, 0, "canceled"),
        (10, 0, 0, 0, "closed"),
        (0, 0, 10, 0, "rejected"),
    ],
)
def test_cumulative_order_status_requires_whole_order_evidence(
    fill, cancel, reject, remaining, status
):
    result = parse_order(
        synthetic_order(
            tot_ccld_qty=str(fill),
            tot_ccld_amt=str(fill * 10000),
            cnc_cfrm_qty=str(cancel),
            rjct_qty=str(reject),
            rmn_qty=str(remaining),
            avg_prvs="9999.9",
        ),
        "test-scope",
    )
    assert result["status"] == status
    assert result["filled"] == fill and result["remaining"] == remaining
    assert result["average"] == (10000 if fill else None)
    assert result["fee"] is None
    assert result["id"].startswith("test-scope:20261022:")


@pytest.mark.parametrize(
    "changes",
    [
        {"ord_qty": "1.5"},
        {"tot_ccld_qty": "3", "tot_ccld_amt": "30000"},
        {"tot_ccld_qty": "1", "tot_ccld_amt": "0", "rmn_qty": "9"},
        {"sll_buy_dvsn_cd": "99"},
        {"ord_dvsn_cd": "12"},
        {"pdno": "005930/KRW"},
    ],
)
def test_inconsistent_or_unsupported_broker_orders_fail(changes):
    with pytest.raises(ccxt.BaseError):
        parse_order(synthetic_order(**changes), "test-scope")


def test_synthetic_watermarks_are_atomic_deduplicated_and_survive_restart(store):
    first = parse_order(synthetic_order(), store.scope)
    store.observe(first, 1000)
    second = parse_order(
        synthetic_order(tot_ccld_qty="2", tot_ccld_amt="20000", rmn_qty="8"), store.scope
    )
    store.observe(second, 2000)
    store.observe(second, 2001)
    trades = store.trades()
    assert len(trades) == 1
    assert trades[0]["info"] == {
        "synthetic": True,
        "timestamp_kind": "observation",
        "intervalStart": 1000,
        "intervalEnd": 2000,
        "recoveredAggregate": False,
    }
    assert trades[0]["amount"] == 2 and trades[0]["cost"] == 20000
    url = "sqlite:///" + store.db.execute("PRAGMA database_list").fetchone()[2]
    store.close()
    recovered = KisStore(url, store.scope)
    try:
        last = parse_order(
            synthetic_order(tot_ccld_qty="3", tot_ccld_amt="31000", rmn_qty="7"), store.scope
        )
        recovered.observe(last, 3000)
        all_trades = recovered.trades()
        assert all_trades[0]["id"] == trades[0]["id"]
        assert all_trades[1]["amount"] == 1 and all_trades[1]["price"] == 11000
        assert sum(t["amount"] for t in all_trades) == 3
        assert sum(t["cost"] for t in all_trades) == 31000
        assert recovered.trades(since=2500, limit=1) == all_trades[1:]
    finally:
        recovered.close()


def test_unknown_first_fill_is_a_recovered_observation_aggregate(store):
    order = parse_order(
        synthetic_order(tot_ccld_qty="3", tot_ccld_amt="30000", rmn_qty="7"), store.scope
    )
    store.observe(order, 1000)
    assert store.trades()[0]["info"]["intervalStart"] is None
    assert store.trades()[0]["info"]["recoveredAggregate"]


@pytest.mark.parametrize("quantity,cost,remaining", [(1, 10000, 9), (2, 20001, 8), (3, 0, 7)])
def test_invalid_deltas_block_publication_without_erasing_canonical_records(
    store, quantity, cost, remaining
):
    order = parse_order(
        synthetic_order(tot_ccld_qty="2", tot_ccld_amt="20000", rmn_qty="8"), store.scope
    )
    store.observe(order, 1000)
    changed = deepcopy(order)
    changed.update(filled=quantity, cost=cost, remaining=remaining)
    with pytest.raises(ccxt.ExchangeError, match="requires reconciliation"):
        store.observe(changed, 2000)
    assert store.orders()[0]["cost"] == 20000
    assert store.db.execute("SELECT COUNT(*) FROM kis_fills").fetchone()[0] == 1
    with pytest.raises(ccxt.ExchangeError, match="requires reconciliation"):
        store.trades()


def test_fee_only_corrections_do_not_create_fills(store):
    order = parse_order(
        synthetic_order(tot_ccld_qty="2", tot_ccld_amt="20000", rmn_qty="8"), store.scope
    )
    store.observe(order, 1000)
    order["fee"] = {"currency": "KRW", "cost": 10, "rate": 0.0005}
    order["info"]["costStatus"] = "confirmed"
    store.observe(order, 2000)
    assert len(store.trades()) == 1
    assert store.orders()[0]["fee"]["cost"] == 10


def test_failed_snapshot_write_rolls_back_its_fill_and_watermark(store):
    order = parse_order(synthetic_order(), store.scope)
    store.observe(order, 1000)
    store.db.execute("""CREATE TRIGGER fail_snapshot BEFORE INSERT ON kis_observations
                        BEGIN SELECT RAISE(ABORT,'synthetic storage fault'); END""")
    changed = parse_order(
        synthetic_order(tot_ccld_qty="2", tot_ccld_amt="20000", rmn_qty="8"), store.scope
    )
    with pytest.raises(Exception, match="synthetic storage fault"):
        store.observe(changed, 2000)
    assert store.trades() == [] and store.orders()[0]["filled"] == 0
    store.db.execute("DROP TRIGGER fail_snapshot")
    store.observe(changed, 3000)
    assert len(store.trades()) == 1


def test_intents_are_durable_before_transmission_and_exclude_another_owner(store):
    identifier = store.intent("create", "005930/KRW", "buy", "limit", 1, 10000, 10010, 1000)
    assert store.intents()[0]["state"] == "unknown"
    with pytest.raises(ccxt.ExchangeError, match="never resend"):
        store.intent("create", "005930/KRW", "buy", "limit", 1, 10000, 10010, 1001)
    url = "sqlite:///" + store.db.execute("PRAGMA database_list").fetchone()[2]
    with pytest.raises(ccxt.PermissionDenied, match="active account client"):
        KisStore(url, store.scope)
    store.update_intent(identifier, "accepted", "broker-order")
    store.close()
    with pytest.raises(ccxt.PermissionDenied, match="another account"):
        KisStore(url, "another-scope")
