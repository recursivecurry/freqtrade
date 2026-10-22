import json
from pathlib import Path

import pytest

from tests.exchange.kis_adapter.test_client import TOKEN, make_client


CAPTURE = json.loads((Path(__file__).parents[2] / "testdata/kis/flow_contract.json").read_text())


@pytest.mark.parametrize(
    "path,tr,key",
    [
        ("foreign-institution-total", "FHPTJ04400000", "rank_buy"),
        ("inquire-investor", "FHKST01010900", "recent"),
        ("investor-trade-by-stock-daily", "FHPTJ04160001", "daily_flow"),
    ],
)
def test_flow_reads_use_the_serialized_transport(path, tr, key, clock):
    response = CAPTURE["responses"][key]
    api, transport = make_client([TOKEN, response])
    assert api.get("quotations/" + path, tr, {}) == (response, "")
    assert [r.method for r in transport.calls] == ["POST", "GET"]
    assert transport.calls[1].get_header("Tr_id") == tr


def test_captured_rank_amounts_are_broker_price_valuations():
    for key in ("rank_buy", "rank_sell"):
        rows = CAPTURE["responses"][key]["output"]
        assert len(rows) == 30
        assert len({r["mksc_shrn_iscd"] for r in rows}) == len(rows)
        for row in rows:
            for who in ("frgn", "orgn"):
                valuation = int(row[who + "_ntby_qty"]) * int(row["stck_prpr"])
                reported = int(row[who + "_ntby_tr_pbmn"]) * CAPTURE["amount_multiplier"]
                assert abs(valuation - reported) <= CAPTURE["amount_multiplier"]
            assert "stck_bsop_date" not in row
            assert "prsn_ntby_tr_pbmn" not in row


def test_captured_daily_amount_units_match_gross_buy_prices():
    for row in CAPTURE["responses"]["daily_flow"]["output2"]:
        average = (
            int(row["frgn_shnu_tr_pbmn"]) * CAPTURE["amount_multiplier"] / int(row["frgn_shnu_vol"])
        )
        assert int(row["stck_lwpr"]) <= average <= int(row["stck_hgpr"])
        assert int(row["frgn_ntby_tr_pbmn"]) == int(row["frgn_shnu_tr_pbmn"]) - int(
            row["frgn_seln_tr_pbmn"]
        )
