"""Offline execution-policy matrix against immutable real JQ captures.

The native engine consumes only the independently frozen AD input. Platform
observations enter this post-run verifier, never the strategy or price board.
"""

import argparse
import hashlib
import json
from decimal import Decimal
from itertools import product
from pathlib import Path

from ashare_quant.backtests.minute import run_backtest
from tests.fixtures.minute_reference.audit_execution import audit_run
from tests.fixtures.minute_reference.build_scripts import ROOT, generate
from tests.fixtures.minute_reference.compare import (
    INPUT_SHA256,
    NAMES,
    aq_observations,
    audit_closing_marks,
    audit_jq,
    audit_observed_marks,
    near,
    number,
    order_plan,
    read_rows,
    require,
    timestamp,
    validate_rows,
)
from tests.fixtures.research.minute_research.run_demo import CONFIG

LOG_PINS = {
    "shared_cash_t1": "11bdb192c4320227537d2845287ea31d88d741446f7ad3da31c9efea1741f896",
    "visible_momentum": "e85faf64ced90b9cf9cc2755afcfc2f18d73cc413f94aa80d5fce055493b6c4a",
}
ECONOMICS = ("orders", "trades", "accounts", "final_account", "total_pnl", "total_return")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def captures(root, name):
    root = Path(root)
    require(sha(root / f"{name}.logs.jsonl") == LOG_PINS[name], "real JQ capture changed")
    uploaded = (root / f"{name}.uploaded.py").read_bytes()
    require(
        uploaded == (root / f"{name}.backtest-code.py").read_bytes(), "JQ stored source differs"
    )
    require(uploaded.decode() == generate(name, "jq"), "JQ observer source changed")
    meta = json.loads((root / f"{name}.run.json").read_text())
    require(
        meta["status"] == "completed"
        and meta["terminal_complete"]
        and meta["stored_script_equals_upload"],
        "JQ completion not verified",
    )
    rows = read_rows(root / f"{name}.logs.jsonl")
    require(len(rows) == meta["records"], "JQ capture record count changed")
    validate_rows(rows, name, "jq")
    return rows


def compare_aligned(name, root, jq_rows, *, must_align=False):
    """Compare immutable observations, preserving residuals and object differences."""
    root = Path(root)
    result = json.loads((root / "result.json").read_text())
    require(sha(root / "input.json") == INPUT_SHA256, "independent market input changed")
    require((root / "strategy.py").read_text() == generate(name, "aq"), "AQ observer changed")
    require(
        result["historical_pit_verified"] is False and not result["is_test_double"],
        "bad provenance",
    )
    aq_rows = aq_observations(root / "events.jsonl")
    validate_rows(aq_rows, name, "aq")
    validate_rows(jq_rows, name, "jq")
    audits = audit_run(root)
    jq_audit = audit_jq(jq_rows, with_checks=True)
    jq_ledger = jq_audit["ledger"]
    plans = [[order_plan(r) for r in rows if r["kind"] == "submit"] for rows in (aq_rows, jq_rows)]
    require(plans[0] == plans[1], "business order plans changed")
    histories = [
        [
            {k: r[k] for k in ("time", "labels", "close", "scores")}
            for r in rows
            if r["kind"] == "signal_history"
        ]
        for rows in (aq_rows, jq_rows)
    ]
    require(histories[0] == histories[1], "history or selection changed")
    bars = {
        (b["security"], timestamp(b["source_label"])): b
        for b in json.loads((root / "input.json").read_text())["minute"]
    }
    for plan in plans[0]:
        near(
            plan["current_price"],
            bars[plan["security"], timestamp(plan["time"])]["close"],
            "submission quote differs from independent source",
        )
    for history in histories[0]:
        for label in history["labels"]:
            require(timestamp(label) <= timestamp(history["time"]), "future signal history")
        for sid, values in history["close"].items():
            for label, value in zip(history["labels"], values, strict=True):
                near(value, bars[sid, timestamp(label)]["close"], "history source changed")
    aq_ledger = [
        dict(
            time=t["at"],
            security=t["security"],
            side=t["side"],
            amount=t["amount"],
            price=t["price"],
            fees=str(number(t["commission"]) + number(t["tax"])),
            cash_after=t["cash_after"],
        )
        for t in audits["ledger"]
    ]
    if must_align:
        require(len(aq_ledger) == len(jq_ledger), "fill count differs")
        for aq, jq in zip(aq_ledger, jq_ledger, strict=True):
            require(timestamp(aq["time"]) == timestamp(jq["time"]), "fill time differs")
            require(
                all(aq[k] == jq[k] for k in ("security", "side", "amount")), "fill quantity differs"
            )
            for key in ("price", "fees", "cash_after"):
                near(aq[key], jq[key], "fill " + key + " differs")
    returns = [[r for r in rows if r["kind"] == "order_return"] for rows in (aq_rows, jq_rows)]
    snapshots = [
        [r for r in rows if r["kind"] == "snapshot" and r["label"].startswith("after:")]
        for rows in (aq_rows, jq_rows)
    ]
    if must_align:
        for aq, jq in zip(*returns, strict=True):
            require(
                all(aq[k] == jq[k] for k in ("time", "api", "security", "returned_none")),
                "order return decision differs",
            )
            if aq["order"] is not None:
                require(
                    all(aq["order"][k] == jq["order"][k] for k in ("amount", "filled", "is_buy")),
                    "order quantity at return differs",
                )
        for aq, jq in zip(*snapshots, strict=True):
            require(
                (aq["time"], aq["label"]) == (jq["time"], jq["label"]),
                "after-order sequence differs",
            )
            for key in aq["account"]:
                near(
                    aq["account"][key],
                    jq["account"][key],
                    "after-order account " + key + " differs",
                )
            positions = [{p["security"]: p for p in r["positions"]} for r in (aq, jq)]
            require(positions[0].keys() == positions[1].keys(), "after-order holdings differ")
            for sid, p in positions[0].items():
                for key in ("total_amount", "closeable_amount", "avg_cost", "price", "value"):
                    near(p[key], positions[1][sid][key], "after-order position " + key + " differs")
    daily = []
    closes = [
        [r for r in rows if r["kind"] == "snapshot" and r["time"].endswith("15:30:00")]
        for rows in (aq_rows, jq_rows)
    ]
    for aq, jq in zip(*closes, strict=True):
        delta = number(aq["account"]["total_value"]) - number(jq["account"]["total_value"])
        positions = [{p["security"]: p for p in r["positions"]} for r in (aq, jq)]
        marks = sum(
            (
                number(p["total_amount"])
                * (number(p["price"]) - number(positions[1][sid]["price"]))
                for sid, p in positions[0].items()
                if sid in positions[1]
            ),
            Decimal(0),
        )
        if must_align:
            require(
                {s: p["total_amount"] for s, p in positions[0].items()}
                == {s: p["total_amount"] for s, p in positions[1].items()},
                "daily holdings differ",
            )
            near(delta, marks, "daily NAV has a difference beyond the known mark clock")
        daily.append(
            dict(
                date=aq["time"][:10],
                aq=aq["account"],
                jq=jq["account"],
                aq_positions=aq["positions"],
                jq_positions=jq["positions"],
                aq_minus_jq=str(delta),
                mark_clock_difference=str(marks),
                difference_beyond_mark_clock=str(delta - marks),
            )
        )
    difference = number(result["final_account"]["total_value"]) - number(
        jq_rows[-2]["account"]["total_value"]
    )
    if must_align:
        near(difference, 0, "final equity differs")
    require(not result["final_account"]["positions"], "AQ final holdings not flat")
    return dict(
        policies=audits["policies"],
        aq_trades=aq_ledger,
        jq_trades=jq_ledger,
        business_order_plans=plans[0],
        signal_histories=histories[0],
        aq_final=result["final_account"]["total_value"],
        jq_final=jq_rows[-2]["account"]["total_value"],
        aq_minus_jq=str(difference),
        daily_accounts=daily,
        native_audit=audits,
        jq_snapshot_audit=jq_audit,
        aligned_fill_and_after_order_checks=must_align,
        after_order_checks=len(snapshots[0]),
        order_returns={"aq": returns[0], "jq": returns[1]},
        strict_equivalence=False,
        historical_pit_verified=False,
        lifecycle_observations={
            platform: dict(
                initialize_time=rows[0]["time"],
                terminal_snapshot_time=[r["time"] for r in rows if r["kind"] == "snapshot"][-1],
                strategy_end_callbacks=sum(
                    r["kind"] == "snapshot" and r["label"] == "strategy_end" for r in rows
                ),
                handle_data_checkpoints=[
                    r["time"]
                    for r in rows
                    if r["kind"] == "snapshot" and r["label"].startswith("handle_data:")
                ],
            )
            for platform, rows in (("aq", aq_rows), ("jq", jq_rows))
        },
        remaining=[
            "per-order daily-volume initial JQ cap is not implemented or verified",
            "opening/lunch boundary BAR clocks and terminal callback differ",
            "order status/price/avg_cost/commission and trade fields are not identical",
            "get_orders/get_trades retain this run, not only today",
            "resting strict-cross rule has local tests and documentation, no real JQ run",
        ],
    )


def execute(script, input_path, output, config):
    opts = dict(config=config, accept_assumed=True, accept_state_replay=True, accept_research=True)
    run = run_backtest(script, input_path, output, **opts)
    require(run["native_run"], "use a fresh output directory")
    again = run_backtest(script, input_path, output, **opts)
    require(
        not again["native_run"] and again["result"] == run["result"], "completed reentry changed"
    )
    return run["result"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--jq", required=True, type=Path)
    p.add_argument("--boundary", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument(
        "--existing", type=Path, help="read-only re-audit of an existing native policy matrix"
    )
    p.add_argument(
        "--checkpoint-marks", type=Path, help="AD --all-checkpoints export for every JQ mark"
    )
    p.add_argument("--accept-research", action="store_true")
    args = p.parse_args()
    if not args.accept_research:
        p.error("--accept-research must accept the existing TDX profile's bounded assumptions")
    require(sha(args.input) == INPUT_SHA256, "independent market input hash changed")
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(
        schema="aq-jq-minute-execution-alignment.v2",
        input_sha256=INPUT_SHA256,
        jq_log_pins=LOG_PINS,
        cases={},
        new_platform_runs=0,
        native_runs=0 if args.existing else 10,
    )
    for name in NAMES:
        jq = captures(args.jq, name)
        script = args.output / f"{name}.py"
        script.write_text(generate(name, "aq"))
        original = (
            json.loads((args.existing / (name + "-original-default") / "result.json").read_text())
            if args.existing
            else execute(
                ROOT / "tests/fixtures/research/minute_research" / f"{name}.py",
                args.input,
                args.output / (name + "-original-default"),
                CONFIG,
            )
        )
        original_root = (args.existing or args.output) / (name + "-original-default")
        audit_run(original_root)
        matrix = {}
        for matching, cash_policy in product(("next_open", "jq_current"), ("reject", "resize")):
            key = matching + "-" + cash_policy
            root = args.output / (name + "-" + key)
            if args.existing:
                root = args.existing / (name + "-" + key)
            result = (
                json.loads((root / "result.json").read_text())
                if args.existing
                else execute(
                    script,
                    args.input,
                    root,
                    {**CONFIG, "matching": matching, "cash_policy": cash_policy},
                )
            )
            if key == "next_open-reject":
                prior = json.loads((args.jq / "aq-candidate" / name / "result.json").read_text())
                require(
                    all(result[k] == original[k] == prior[k] for k in ECONOMICS),
                    "default economics or observation invariance regressed",
                )
            matrix[key] = compare_aligned(name, root, jq, must_align=key == "jq_current-resize")
        audit_closing_marks(matrix, json.loads(args.boundary.read_text()))
        if args.checkpoint_marks:
            marks = audit_observed_marks(jq, json.loads(args.checkpoint_marks.read_text()))
            for case in matrix.values():
                case["jq_independent_marks"] = marks
        report["cases"][name] = matrix
    report["default_original_and_observed_economics_unchanged"] = True
    report["completed_reentry_native_runs"] = None if args.existing else 0
    (args.output / "alignment.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                name: {
                    k: dict(aq_final=v["aq_final"], aq_minus_jq=v["aq_minus_jq"])
                    for k, v in matrix.items()
                }
                for name, matrix in report["cases"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
