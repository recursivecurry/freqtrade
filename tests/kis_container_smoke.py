"""Offline image smoke with synthetic private responses and public quote fixtures.

Order fields follow KIS examples at 2077773a69a428daf63e148182511f068cc45de3.
No response in this driver establishes real paper-account compatibility.
"""

import argparse
import io
import json
import logging
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import freqtrade
from freqtrade.exchange.kis_adapter import client, market_data, trading
from freqtrade.exchange.kis_adapter.rules import SEOUL
from freqtrade.exchange.kis_adapter.validate import main as validate_main
from freqtrade.util import dt_ts


FIXTURES = Path(__file__).parent / "testdata/kis"
NOW = datetime(2026, 10, 22, 9, 1, 2, tzinfo=SEOUL)


class FixtureBroker:
    def __init__(self, directory):
        self.path = directory / "broker.json"
        self.state = json.loads(self.path.read_text())

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        self.state["requests"].append({"method": request.method, "path": path})
        if path == "/oauth2/tokenP":
            result = {"access_token": "synthetic-token", "expires_in": 86400}
        elif path.endswith("inquire-price"):
            result = json.loads((FIXTURES / "ticker_005930.json").read_text())
        elif path.endswith("inquire-asking-price-exp-ccn"):
            result = json.loads((FIXTURES / "book_005930.json").read_text())["response"]
        elif path.endswith("inquire-daily-ccld"):
            result = {"rt_cd": "0", "output1": self.state["orders"]}
        elif path.endswith("inquire-balance"):
            result = {
                "rt_cd": "0",
                "output1": [
                    {
                        "pdno": "005930",
                        "hldg_qty": str(self.state["held"]),
                        "ord_psbl_qty": str(self.state["held"]),
                    }
                ],
                "output2": [
                    {
                        "dnca_tot_amt": "1000000",
                        "nxdy_excc_amt": "1000000",
                        "prvs_rcdl_excc_amt": "1000000",
                        "tot_loan_amt": "0",
                    }
                ],
            }
        elif path.endswith("inquire-psbl-order"):
            result = {"rt_cd": "0", "output": {"nrcvb_buy_amt": "1000000", "nrcvb_buy_qty": "100"}}
        elif path.endswith("inquire-psbl-rvsecncl"):
            result = {
                "rt_cd": "0",
                "output": [
                    {
                        "odno": row["odno"],
                        "ord_gno_brno": row["ord_gno_brno"],
                        "psbl_qty": row["rmn_qty"],
                    }
                    for row in self.state["orders"]
                    if int(row["rmn_qty"])
                ],
            }
        elif path.endswith("order-cash"):
            body = json.loads(request.data)
            row = {
                "pdno": body["PDNO"],
                "ord_dt": "20261022",
                "ord_tmd": "090102",
                "ord_gno_brno": "12345",
                "odno": "1001",
                "sll_buy_dvsn_cd": "02",
                "ord_dvsn_cd": body["ORD_DVSN"],
                "ord_qty": body["ORD_QTY"],
                "ord_unpr": body["ORD_UNPR"],
                "tot_ccld_qty": "0",
                "tot_ccld_amt": "0",
                "avg_prvs": "0",
                "rmn_qty": body["ORD_QTY"],
                "cnc_cfrm_qty": "0",
                "rjct_qty": "0",
            }
            self.state["orders"].append(row)
            result = {
                "rt_cd": "0",
                "output": {"ODNO": "1001", "KRX_FWDG_ORD_ORGNO": "12345", "ORD_TMD": "090102"},
            }
        elif path.endswith("order-rvsecncl"):
            row = self.state["orders"][0]
            row.update(cnc_cfrm_qty=row["rmn_qty"], rmn_qty="0")
            result = {"rt_cd": "0", "output": {"ODNO": "1002"}}
        else:
            raise AssertionError("Unexpected synthetic endpoint")
        self.path.write_text(json.dumps(self.state))
        response = io.BytesIO(json.dumps(result).encode())
        response.headers = {}
        return response


def run_step(directory, step):
    broker = FixtureBroker(directory)
    args = [
        "--config",
        str(directory / "config.json"),
        "--report",
        str(directory / (step + ".json")),
    ]
    if step == "submit":
        args += [
            "--action",
            "submit",
            "--allow-paper-orders",
            "--side",
            "buy",
            "--amount",
            "10",
            "--price",
            "10000",
        ]
    elif step == "cancel":
        identifier = json.loads((directory / "submit.json").read_text())["submitted_order"]
        args += ["--action", "cancel", "--allow-paper-orders", "--order-id", identifier]
    elif step != "preflight":
        args += ["--action", "reconcile"]
    with (
        patch("socket.socket.connect", side_effect=AssertionError("External network forbidden")),
        patch.object(client, "build_opener", return_value=broker),
        patch.object(
            client, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: None)
        ),
        patch.object(
            market_data,
            "_master_bytes",
            side_effect=lambda board, day: "\n".join(
                json.loads((FIXTURES / ("master_" + board.lower() + "_subset.json")).read_text())
            ).encode("cp949"),
        ),
        patch("freqtrade.exchange.kis.dt_now", return_value=NOW),
        patch.object(trading, "dt_now", return_value=NOW),
        patch.object(trading, "dt_ts", side_effect=lambda value=None: dt_ts(value or NOW)),
    ):
        return validate_main(args)


def run_scenario():
    with tempfile.TemporaryDirectory(prefix="kis-smoke-") as temp:
        directory = Path(temp)
        config = json.loads(
            (
                Path(freqtrade.__file__).parent.parent / "config_examples/config_kis.example.json"
            ).read_text()
        )
        config.update(user_data_dir=temp, db_url="sqlite:///" + str(directory / "paper.sqlite"))
        config["exchange"].update(
            api_key="synthetic-key", secret="synthetic-secret", account_id="12345678", uid="01"
        )
        config["exchange"]["kis"].update(
            sessions={
                "2026-10-22": {
                    "open": "09:00",
                    "auction": "15:20",
                    "close": "15:30",
                    "source": "synthetic smoke schedule",
                    "verified_at": NOW.isoformat(),
                }
            },
            costs={
                "005930/KRW": [
                    {
                        "from": "2026-01-01",
                        "through": "2026-12-31",
                        "buy_commission": 0.001,
                        "sell_commission": 0.001,
                        "sell_tax": 0.002,
                        "source": "synthetic smoke costs",
                        "tax_treatment": "stock_transaction_tax",
                    }
                ]
            },
            settlement_calendar={
                (NOW.date() + timedelta(days=i)).isoformat(): {
                    "settles": i in (1, 4),
                    "source": "synthetic smoke settlement calendar",
                }
                for i in range(1, 5)
            },
        )
        (directory / "config.json").write_text(json.dumps(config))
        (directory / "broker.json").write_text(
            json.dumps({"orders": [], "held": 0, "requests": []})
        )
        reports = {}
        for step in ("preflight", "submit", "after_fill", "cancel", "after_restart"):
            if step == "after_fill":
                state = json.loads((directory / "broker.json").read_text())
                state["held"] = 3
                state["orders"][0].update(
                    tot_ccld_qty="3", tot_ccld_amt="30000", avg_prvs="10000", rmn_qty="7"
                )
                (directory / "broker.json").write_text(json.dumps(state))
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--step",
                    step,
                    "--directory",
                    temp,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                report_path = directory / (step + ".json")
                failure = report_path.read_text() if report_path.exists() else "no report"
                raise AssertionError(step + " failed: " + result.stdout + result.stderr + failure)
            report = json.loads((directory / (step + ".json")).read_text())
            assert report["status"] == "passed" and not report["phase6_complete"], report
            reports[step] = report
        final = reports["after_restart"]
        assert final["orders"][0]["status"] == "canceled"
        assert final["orders"][0]["filled"] == 3 and final["orders"][0]["remaining"] == 0
        assert final["positions"] == {"005930": 3} and final["cash"]["total"] == 969970
        assert final["synthetic_fill_count"] == 1
        mutations = sum(report["broker_mutation_attempts"] for report in reports.values())
        assert mutations == 2
        return {
            "environment": "offline_synthetic",
            "status": "passed",
            "processes": len(reports),
            "versions": final["versions"],
            "source_hashes": final["source_hashes"],
            "orders": final["orders"],
            "cash": final["cash"],
            "positions": final["positions"],
            "synthetic_fill_count": final["synthetic_fill_count"],
            "synthetic_mutation_attempts": mutations,
            "phase6_complete": False,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step")
    parser.add_argument("--directory", type=Path)
    options = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if options.step:
        raise SystemExit(run_step(options.directory, options.step))
    print(json.dumps(run_scenario(), indent=2))
