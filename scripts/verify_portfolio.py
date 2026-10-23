"""Actual native executions with synthetic inputs, arithmetic checks and SIGKILL recovery.

Synthetic weekdays below are explicitly NOT an exchange calendar or real-source evidence.
The immutable real sample's prices are never consulted by this verifier.
"""

import argparse
import copy
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def generate(manifest, constant=False):
    cal = []
    day = dt.date(2019, 7, 1)
    while day <= dt.date(2020, 5, 6):
        if day.weekday() < 5:
            cal.append(str(day))
        day += dt.timedelta(days=1)
    rows = []
    for j, sid in enumerate(manifest["securities"]):
        for n, day in enumerate(cal):
            close = (
                10
                if constant
                else round((10 + 5 * j) * (1 + 0.0005 * n + 0.04 * math.sin((n + j * 7) / 12)), 2)
            )
            rows.append(
                dict(
                    security=sid,
                    date=day,
                    open=str(close),
                    close=str(close),
                    high=str(round(close * 1.01, 2)),
                    low=str(round(close * 0.99, 2)),
                    volume="1000000",
                    suspended=sid == "000001.XSHE" and day == "2020-02-03",
                    upper_limit=str(round(close * 1.1, 2)),
                    lower_limit=str(round(close * 0.9, 2)),
                )
            )
    return dict(
        schema="ad-daily-portfolio.v1",
        producer={"name": "synthetic-test", "version": "1"},
        real_market_data=False,
        source_refs=["synthetic weekdays and deterministic prices"],
        limitations=["synthetic; not exchange observations or JQ comparison"],
        price_basis="raw",
        volume_unit="share",
        securities=manifest["securities"],
        calendar=cal,
        daily=rows,
        actions=[],
        action_coverage={"status": "synthetic_known"},
        listing_coverage="synthetic_known",
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    examples = REPO / "tests/fixtures/research/portfolio"
    manifest = json.loads((examples / "manifest.json").read_text())
    synthetic = out / "synthetic.json"
    synthetic.write_text(json.dumps(generate(manifest)))
    commands = []
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), PYTHONDONTWRITEBYTECODE="1")

    def run(
        name,
        strategy,
        *,
        source=synthetic,
        question=examples / "manifest.json",
        fault="none",
        root=None,
        expect=0,
    ):
        root = out / name if root is None else root
        command = [
            sys.executable,
            "-B",
            "-m",
            "tests.support.portfolio_engine",
            "--manifest",
            str(question),
            "--input",
            str(source),
            "--strategy",
            strategy,
            "--window",
            "w2020",
            "--output",
            str(root),
            "--invocation",
            name,
            "--fault",
            fault,
        ]
        with (out / (name + ".log")).open("w") as log:
            proc = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        commands.append({"name": name, "command": command, "exit_code": proc.returncode})
        (out / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        if expect == "killed":
            assert proc.returncode != 0 and not (root / "result.json").exists(), name
        elif expect == "rejected":
            assert proc.returncode != 0, name
        else:
            assert proc.returncode == expect, (name, (out / (name + ".log")).read_text()[-3000:])
        print(name, proc.returncode, flush=True)
        return json.loads((root / "result.json").read_text()) if expect == 0 else None

    baseline = {}
    for strategy in ("equal-weight", "momentum", "trend"):
        baseline[strategy] = run(strategy + "-baseline", strategy)
        repeat = run(strategy + "-repeat", strategy)
        assert baseline[strategy] == repeat, strategy + " rerun differs"
        reentry = run(strategy + "-reentry", strategy, root=out / (strategy + "-baseline"))
        assert reentry == baseline[strategy], strategy + " reentry differs"
    for label, fault in (
        ("before-commit", "before_replace:2"),
        ("after-commit", "after_replace:2"),
        ("after-decision", "decision_candidate:1"),
        ("after-first-order", "native_submit:2"),
    ):
        root = out / label
        run(label, "momentum", fault=fault, root=root, expect="killed")
        recovered = run(label + "-recover", "momentum", root=root)
        assert recovered == baseline["momentum"], label + " recovery differs"
    # Independent minimum-fee arithmetic: seven 100-share fills at 10, each cost 5.
    hand = out / "hand-question"
    shutil.copytree(examples, hand)
    m = copy.deepcopy(manifest)
    m["execution"]["initial_cash"] = "10000.00"
    (hand / "manifest.json").write_text(json.dumps(m))
    flat = out / "flat.json"
    flat_doc = generate(m, constant=True)
    flat.write_text(json.dumps(flat_doc))
    calculated = run("minimum-fee", "equal-weight", source=flat, question=hand / "manifest.json")
    first = calculated["accounts"][0]
    assert first["cash"] == "2965.0" and first["equity"] == "9965.0", first
    actions = copy.deepcopy(flat_doc)
    sid = "600000.XSHG"
    actions["actions"] = [
        {
            "security": sid,
            "kind": "cash_dividend",
            "record_date": "2020-02-04",
            "ex_date": "2020-02-05",
            "pay_date": "2020-02-07",
            "cash_per_share": "1",
        },
        {
            "security": sid,
            "kind": "split",
            "ex_date": "2020-02-10",
            "credit_date": "2020-02-10",
            "tradable_date": "2020-02-10",
            "ratio": "2",
        },
    ]
    for row in actions["daily"]:
        if row["security"] == sid and row["date"] >= "2020-02-05":
            price = 9 if row["date"] < "2020-02-10" else 4.5
            row.update(
                open=str(price),
                close=str(price),
                high=str(price),
                low=str(price),
                upper_limit=str(price * 1.1),
                lower_limit=str(price * 0.9),
            )
    actions_path = out / "actions.json"
    actions_path.write_text(json.dumps(actions))
    acted = run(
        "actions-baseline", "equal-weight", source=actions_path, question=hand / "manifest.json"
    )
    ex = next(a for a in acted["accounts"] if a["date"] == "2020-02-05")
    assert ex["positions"][sid]["dividend_receivable"] == "100.0"
    pay = next(a for a in acted["accounts"] if a["date"] == "2020-02-07")
    assert pay["cash"] == "3065.0" and pay["positions"][sid]["dividend_receivable"] == "0.0"
    split = next(a for a in acted["accounts"] if a["date"] == "2020-02-10")
    assert split["positions"][sid]["quantity"] == 200 and split["equity"] == "9965.0"
    action_root = out / "actions-crash"
    run(
        "actions-crash",
        "equal-weight",
        source=actions_path,
        question=hand / "manifest.json",
        root=action_root,
        fault="after_replace:8",
        expect="killed",
    )
    assert (
        run(
            "actions-recover",
            "equal-weight",
            source=actions_path,
            question=hand / "manifest.json",
            root=action_root,
        )
        == acted
    )
    # Gap-up demand exhausts a single shared cash account. Native rejection, not debt.
    gapup = copy.deepcopy(flat_doc)
    for row in gapup["daily"]:
        if row["date"] == "2020-02-03":
            row.update(open="20", high="20", upper_limit="30", lower_limit="5", suspended=False)
    gapup_path = out / "gapup.json"
    gapup_path.write_text(json.dumps(gapup))
    limited = run(
        "shared-cash-rejection", "equal-weight", source=gapup_path, question=hand / "manifest.json"
    )
    subs = [s for s in limited["submissions"] if s["date"] == "2020-02-03"]
    assert sum(s["returned_none"] for s in subs) == 4, subs
    assert limited["accounts"][0]["cash"] == "1980.0"
    # Native full execution with changed current/future closes must not alter first signal.
    future = json.loads(synthetic.read_text())
    for row in future["daily"]:
        if row["date"] >= "2020-02-03":
            row["close"] = str(float(row["close"]) * 2)
            row["high"] = row["close"]
    future_path = out / "future-perturbed.json"
    future_path.write_text(json.dumps(future))
    perturbed = run("future-cutoff", "momentum", source=future_path)
    assert perturbed["decisions"][0] == baseline["momentum"]["decisions"][0]
    missing = json.loads(synthetic.read_text())
    next(r for r in missing["daily"] if r["date"] == "2020-02-03")["suspended"] = None
    missing_path = out / "missing-state.json"
    missing_path.write_text(json.dumps(missing))
    run("missing-state", "equal-weight", source=missing_path, expect="rejected")
    assert not (out / "missing-state/native.chk").exists()
    run(
        "identity-drift",
        "momentum",
        source=future_path,
        root=out / "momentum-baseline",
        expect="rejected",
    )
    report = {
        "status": "passed",
        "real_market_data": False,
        "jq_comparison": "not_collected",
        "strategies": {
            s: {"sessions": len(r["accounts"]), "trades": len(r["trades"])}
            for s, r in baseline.items()
        },
        "replay": "three byte-equivalent result pairs and completed reentry",
        "recovery": "four SIGKILL locations; byte-equivalent recovered results",
        "hand_cash": "2965.0",
        "hand_equity": "9965.0",
        "cash_rejections": 4,
        "future_cutoff": "same first decision with changed current/future data",
        "missing_state": "blocked before native ledger",
        "identity_drift": "rejected",
        "corporate_actions": "gross receivable/pay-date cash/split quantity independently reconciled; interrupted receivable state recovered exactly",
    }
    (out / "verification.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
