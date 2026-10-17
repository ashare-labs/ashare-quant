"""Offline synthetic native regression for gross entitlement versus price deduction.

No owner package, JQ capture, network, account identifier or historical prices are
needed. Deliberately wrong input coefficients are controls, never data repairs.
The original real-strategy manifest and its samples are not modified.
"""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from verify_portfolio import generate

REPO = Path(__file__).resolve().parents[1]
SID = "600000.XSHG"
RECORD, EX, LATER_PAY = "2020-02-04", "2020-02-05", "2020-02-07"
TOLERANCE = Decimal("0.000001")


def close(actual, expected):
    assert abs(Decimal(str(actual)) - Decimal(str(expected))) <= TOLERANCE, (actual, expected)


def account(result, day):
    return next(row for row in result["accounts"] if row["date"] == day)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    out = parser.parse_args().output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    question = out / "synthetic-question"
    shutil.copytree(REPO / "examples/portfolio", question)
    manifest = json.loads((question / "manifest.json").read_text())
    manifest["purpose"] = "Synthetic dividend field-boundary regression, not a real market run"
    manifest["windows"] = [{"id": "w2020", "start": "2020-02-03", "end": LATER_PAY}]
    (question / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    data = generate(manifest, constant=True)
    data["source_refs"] = ["synthetic prices, weekdays and dividend coefficients"]
    for row in data["daily"]:
        price = "34.06" if row["security"] == SID and row["date"] >= EX else "35"
        row.update(
            open=price,
            high=price,
            low=price,
            close=price,
            suspended=False,
            upper_limit="50",
            lower_limit="20",
        )
    data["actions"] = [
        {
            "security": SID,
            "kind": "cash_dividend",
            "record_date": RECORD,
            "ex_date": EX,
            "pay_date": EX,
            "cash_per_share": "1",
            "source_event": {
                "origin": "synthetic_fixture",
                "cash_per_share": "1",
                "ex_price_cash_deduction": "0.936124",
                "payment_time": None,
                "bonus_share_ratio": "0",
                "capitalization_share_ratio": "0",
            },
        }
    ]
    commands = []
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), PYTHONDONTWRITEBYTECODE="1")

    def run(name, document, *, root=None, fault="none", killed=False):
        source = out / (name + "-input.json")
        source.write_text(json.dumps(document, indent=2) + "\n")
        target = root or out / name
        command = [
            sys.executable,
            "-B",
            "-m",
            "ashare_quant.portfolio",
            "--manifest",
            str(question / "manifest.json"),
            "--input",
            str(source),
            "--strategy",
            "equal-weight",
            "--window",
            "w2020",
            "--output",
            str(target),
            "--invocation",
            name,
            "--fault",
            fault,
        ]
        with (out / (name + ".log")).open("w") as log:
            proc = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        commands.append({"name": name, "command": command, "exit_code": proc.returncode})
        (out / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        if killed:
            assert proc.returncode in {-9, 247} and not (target / "result.json").exists()
            return None
        assert proc.returncode == 0, (name, (out / (name + ".log")).read_text()[-2000:])
        assert json.loads((target / "audit.json").read_text())["passed"]
        return json.loads((target / "result.json").read_text())

    baseline = run("gross-entitlement", data)
    before, after = account(baseline, RECORD), account(baseline, EX)
    close(before["cash"], "19706")  # Eight fills: 3500 * 35 + 36.75 commission each.
    close(after["cash"], "23206")
    close(after["positions"][SID]["average_price"], "34")
    assert after["positions"][SID]["quantity"] == after["positions"][SID]["sellable"] == 3500
    assert len(baseline["trades"]) == 8 and all(
        t["date"] == "2020-02-03" for t in baseline["trades"]
    )
    assert baseline["native_account_owner"] == "rqalpha.portfolio.account"

    # Metadata alone is not a cash entitlement. Changing it must not change any ledger field.
    metadata_only = copy.deepcopy(data)
    metadata_only["actions"][0]["source_event"]["ex_price_cash_deduction"] = "0.75"
    altered = run("different-reference-metadata", metadata_only)
    for key in ("decisions", "orders", "trades", "accounts", "submissions", "final_account"):
        assert altered[key] == baseline[key], key

    # Controls deliberately use two other coefficients as gross cash, with zero withholding.
    # The second coefficient equals 0.9361 * (1 - 0.20). No tax implementation is being added.
    controls = {}
    for name, coefficient in [("rounded-reference-as-cash", "0.9361"), ("net-as-gross", "0.74888")]:
        wrong = copy.deepcopy(data)
        wrong["actions"][0]["cash_per_share"] = coefficient
        wrong["actions"][0]["source_event"]["cash_per_share"] = coefficient
        result = run(name, wrong)
        snapshot = account(result, EX)
        close(Decimal(snapshot["cash"]) - Decimal(before["cash"]), Decimal(coefficient) * 3500)
        close(snapshot["positions"][SID]["average_price"], Decimal(35) - Decimal(coefficient))
        assert result["trades"] == baseline["trades"]
        controls[name] = snapshot
    gross_basis_gap = Decimal(after["cash"]) - Decimal(
        controls["rounded-reference-as-cash"]["cash"]
    )
    gross_net_gap = Decimal(controls["rounded-reference-as-cash"]["cash"]) - Decimal(
        controls["net-as-gross"]["cash"]
    )
    close(gross_basis_gap, "223.65")
    close(gross_net_gap, "655.27")
    close(gross_basis_gap + gross_net_gap, "878.92")
    # Matching net cash by substituting input does not preserve gross-based cost adjustment.
    close(controls["net-as-gross"]["positions"][SID]["average_price"], "34.25112")
    assert (
        controls["net-as-gross"]["positions"][SID]["average_price"]
        != controls["rounded-reference-as-cash"]["positions"][SID]["average_price"]
    )

    # A different payment date changes receivable/cash, not entitlement or shares.
    delayed = copy.deepcopy(data)
    delayed["actions"][0]["pay_date"] = LATER_PAY
    deferred = run("deferred-payment", delayed)
    ex_snapshot, pay_snapshot = account(deferred, EX), account(deferred, LATER_PAY)
    close(ex_snapshot["cash"], before["cash"])
    close(ex_snapshot["positions"][SID]["dividend_receivable"], "3500")
    close(ex_snapshot["equity"], after["equity"])
    close(pay_snapshot["cash"], after["cash"])
    close(pay_snapshot["positions"][SID]["dividend_receivable"], "0")
    crash = out / "deferred-crash"
    run("deferred-crash", delayed, root=crash, fault="after_replace:8", killed=True)
    assert run("deferred-recover", delayed, root=crash) == deferred
    report = {
        "schema": "aq.synthetic-dividend-boundary.v1",
        "passed": True,
        "real_market_data": False,
        "jq_equivalence_claimed": False,
        "native_account_owner": baseline["native_account_owner"],
        "scenario": {
            "security": SID,
            "quantity": 3500,
            "gross_per_share": "1",
            "price_reference_metadata": "0.936124",
            "withholding_rate": "0",
        },
        "cash_credit": {
            "gross": "3500",
            "rounded_reference_control": "3276.35",
            "net_coefficient_control": "2621.08",
        },
        "gaps": {"gross_basis": "223.65", "gross_to_net": "655.27", "total": "878.92"},
        "reference_metadata_does_not_change_cash_or_cost": True,
        "same_trades_and_quantities": True,
        "net_substitution_changes_cost_basis": True,
        "delayed_payment_preserves_receivable_and_equity": True,
        "interrupted_receivable_recovery_equal": True,
        "payment_intraday_time_proven": False,
        "historical_tax_treatment_proven": False,
    }
    (out / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
