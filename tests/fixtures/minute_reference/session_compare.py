"""Run and exhaustively compare three frozen scripts on the reviewed AD session.

JQ observations are read only after native execution. Every observation field is
checked; the small, named source/API differences remain in the output report.
This is a two-stock retrospective experiment, not a general equivalence claim.
"""

import argparse
import json
import re
from collections import Counter
from decimal import localcontext
from pathlib import Path

from tests.fixtures.minute_reference.align import captures, execute
from tests.fixtures.minute_reference.audit_execution import audit_run
from tests.fixtures.minute_reference.audit_gaps import TDX_SHA256, audit_trace, load_capture
from tests.fixtures.minute_reference.build_scripts import NAMES, ROOT, generate
from tests.fixtures.minute_reference.compare import (
    aq_observations,
    audit_jq,
    near,
    number,
    read_rows,
    require,
    timestamp,
    validate_rows,
)
from tests.fixtures.minute_reference.session_input import (
    AD_COMMIT,
    AD_EXPORT_SHA256,
    AD_WHEEL_SHA256,
    FULL_INPUT_SHA256,
    project,
    sha,
)

CASES = (*NAMES, "gap_ported")


def config(name):
    return dict(
        lifecycle="jq_minute",
        matching="jq_current",
        capacity_policy="jq_daily_initial",
        cash_policy="resize",
        initial_cash=10000000 if name == "gap_ported" else 100000,
        volume_percent=0.0001 if name == "gap_ported" else 0.25,
        commission_rate=0.0003,
        min_commission=5,
        sell_tax=0.0005,
    )


def observations(root, name):
    path = Path(root) / "events.jsonl"
    if name in NAMES:
        return aq_observations(path)
    return [
        json.loads(e["message"].split(" ", 1)[1])
        for e in read_rows(path)
        if e["kind"] == "log" and e["message"].startswith("AQGAP_JSON ")
    ]


def canonical_ids(rows):
    """Bind each opaque ID once, in first-observed order; retain every reference."""
    orders, trades = {}, {}

    def visit(value, key=""):
        if key in {"order_id", "trade_id"}:
            ids = orders if key == "order_id" else trades
            ids.setdefault(str(value), str(len(ids) + 1))
            return ids[str(value)]
        if key == "open_orders":
            require(all(str(v) in orders for v in value), "Open order precedes identity")
            return [orders[str(v)] for v in value]
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value

    return [visit(row) for row in rows]


def documented_difference(path, aq, jq, rows):
    unavailable = {"unavailable": "AttributeError"}
    if path == "/0/platform" and aq == "aq" and jq == "jq":
        return "platform_identity"
    if re.fullmatch(r"/\d+/trades/\d+/(at|commission|side|tax)", path) and jq == unavailable:
        return "additional_AQ_trade_attribute_not_exposed_by_JQ"
    if (
        re.fullmatch(r"/\d+/(market|securities/[^/]+/current)/(high_limit|low_limit)", path)
        and aq == {"unavailable": "ValueError"}
        and isinstance(jq, (int, float))
    ):
        return "AD_source_has_no_verified_exchange_price_limit"
    match = re.fullmatch(r"/(\d+)/style/(price|_limit_price)", path)
    if match:
        row = rows[int(match[1])]
        if match[2] == "price" and jq == unavailable and aq == row["style"]["price"]:
            return "limit_style_public_private_attribute_difference"
        if match[2] == "_limit_price" and aq == unavailable and jq == row["style"]["price"]:
            return "limit_style_public_private_attribute_difference"
    if re.fullmatch(r"/\d+/(securities/[^/]+/history/\d+|rows/0)/volume", path):
        if isinstance(aq, (int, float)) and isinstance(jq, (int, float)):
            return "unmodified_TDX_JQ_source_volume_difference"
    return None


def compare_rows(aq, jq):
    """Fail on every unexplained field, missing record or collection member."""
    aq, jq = canonical_ids(aq), canonical_ids(jq)
    differences, checked = [], 0

    def visit(a, b, path=""):
        nonlocal checked
        if not isinstance(a, (dict, list)) and not isinstance(b, (dict, list)):
            if type(a) in (int, float) and type(b) in (int, float):
                if abs(number(a) - number(b)) < number("0.0000001"):
                    checked += 1
                    return
            elif type(a) is type(b) and a == b:
                checked += 1
                return
        reason = documented_difference(path, a, b, aq)
        if reason:
            differences.append(dict(path=path, aq=a, jq=b, reason=reason))
            return
        if isinstance(a, dict) and isinstance(b, dict):
            require(a.keys() == b.keys(), "Observation keys differ: " + path)
            for key in a:
                visit(a[key], b[key], path + "/" + key)
        elif isinstance(a, list) and isinstance(b, list):
            require(len(a) == len(b), "Observation collection size differs: " + path)
            for index, (x, y) in enumerate(zip(a, b, strict=True)):
                visit(x, y, path + "/" + str(index))
        elif type(a) in (int, float) and type(b) in (int, float):
            near(a, b, "Observation numeric mismatch: " + path)
            checked += 1
        else:
            require(type(a) is type(b) and a == b, "Observation mismatch: " + path)
            checked += 1

    visit(aq, jq)
    return dict(
        equal_leaf_fields=checked,
        difference_counts=dict(Counter(d["reason"] for d in differences)),
        documented_differences=differences,
        unexplained_differences=0,
    )


def verify_source_observations(rows, document):
    """Check all queried values against AD roles, including the history partition."""
    obs = document["session_observations"]
    raw = document["minute"] + obs["history_minutes"]
    bars = {(b["security"], timestamp(b["source_label"])): b for b in raw}
    opening = {(b["security"], b["date"]): b for b in obs["opening_prices"]}
    daily = {(b["security"], b["date"]): b for b in obs["daily"]}
    history_checks, current_checks, daily_checks = 0, 0, 0

    def current(sid, value, at):
        nonlocal current_checks
        day = str(at.date())
        today = [t for s, t in bars if s == sid and t.date() == at.date() and t <= at]
        op = opening[sid, day]
        require(timestamp(op["replay_available_at"]) <= at, "Future opening scalar")
        price = bars[sid, max(today)]["close"] if today else op["price"]
        near(value["last_price"], price, "AD current close differs")
        near(value["day_open"], op["price"], "AD current opening differs")
        current_checks += 2

    for row in rows:
        at = timestamp(row["time"])
        if row["kind"] == "signal_history":
            for sid, values in row["close"].items():
                for label, value in zip(row["labels"], values, strict=True):
                    t = timestamp(label)
                    require(t <= at, "Future signal minute")
                    near(value, bars[sid, t]["close"], "AD signal close differs")
                    history_checks += 1
        elif row["kind"] == "market":
            for sid, value in row["securities"].items():
                current(sid, value["current"], at)
                require("history_error" not in value, "History query failed")
                for minute in value["history"]:
                    t = timestamp(minute["label"])
                    require(t <= at, "Future observed minute")
                    source = bars[sid, t]
                    require(timestamp(source["replay_available_at"]) <= at, "Minute unavailable")
                    for field in ("open", "high", "low", "close", "volume"):
                        near(minute[field], source[field], "AD history differs: " + field)
                        history_checks += 1
        elif row["kind"] == "submit":
            current(row["security"], row.get("current", row.get("market")), at)
        elif row["kind"] == "closed_daily":
            source = daily[row["security"], str(at.date())]
            require(timestamp(source["replay_available_at"]) <= at, "Future daily observation")
            require(len(row["rows"]) == 1, "Daily record count")
            for field in ("close", "volume"):
                near(row["rows"][0][field], source[field], "AD daily differs: " + field)
                daily_checks += 1
    return dict(
        history_fields=history_checks, current_fields=current_checks, daily_fields=daily_checks
    )


def compare_case(name, root, jq, gap_reference):
    # Native libraries can lower ambient Decimal precision. All independent
    # platform cash and source calculations need their own stable context.
    with localcontext() as context:
        context.prec = 50
        return _compare_case(name, root, jq, gap_reference)


def _compare_case(name, root, jq, gap_reference):
    root = Path(root)
    require(sha(root / "input.json") == FULL_INPUT_SHA256, "Frozen full-session input changed")
    require(
        json.loads((root / "manifest.json").read_bytes())["config"] == config(name),
        "Full-session execution policy changed",
    )
    result = json.loads((root / "result.json").read_bytes())
    document = json.loads((root / "input.json").read_bytes())
    require(result["real_market_data"] and not result["is_test_double"], "Real native run required")
    require(result["historical_pit_verified"] is False, "Historical PIT claim changed")
    require(result["execution_policy"]["terminal_day_queries"] == "empty", "Terminal scope differs")
    aq = observations(root, name)
    if name in NAMES:
        platform = captures(jq, name)
        validate_rows(aq, name, "aq")
        platform_audit = audit_jq(platform, with_checks=True)
        require(
            (root / "strategy.py").read_text() == generate(name, "aq"), "Observer script changed"
        )
    else:
        platform = load_capture(gap_reference)
        require(
            sha(Path(gap_reference) / "tdx-full-days.json") == TDX_SHA256,
            "Raw audit source changed",
        )
        raw = json.loads((Path(gap_reference) / "tdx-full-days.json").read_bytes())
        platform_audit = audit_trace(platform, raw)
        original = (ROOT / "tests/fixtures/minute_reference/gap_probe.py").read_text()
        require(
            (root / "strategy.py").read_text()
            == original.replace(
                "skip_paused=False, df=True)", "skip_paused=False, df=True, fq=None)"
            ),
            "Gap migration changed beyond explicit raw-price argument",
        )
    native_audit = audit_run(root)
    comparison = compare_rows(aq, platform)
    source_checks = verify_source_observations(aq, document)
    events = read_rows(root / "events.jsonl")
    handles = Counter(
        e["at"][:10] for e in events if e["kind"] == "callback_start" and e["name"] == "handle_data"
    )
    require(handles == {day: 240 for day in document["days"]}, "Full handle_data coverage")
    zeros = {
        (b["security"], timestamp(b["source_label"]))
        for b in document["minute"]
        if number(b["volume"]) == 0
    }
    require(len(zeros) == 4, "Real zero-volume rows missing")
    require(
        not any((t["security"], timestamp(t["at"])) in zeros for t in result["trades"]),
        "Zero-volume fill",
    )
    near(
        result["final_account"]["total_value"],
        platform[-2]["account"]["total_value"],
        "Final equity differs",
    )
    return dict(
        **comparison,
        observation_records=len(aq),
        source_checks=source_checks,
        native_audit=native_audit,
        jq_audit=platform_audit,
        handles_per_day=dict(handles),
        zero_volume_rows_retained=4,
        zero_volume_fills=0,
        orders=len(result["orders"]),
        trades=len(result["trades"]),
        aq_final=result["final_account"],
        jq_final=platform[-2]["account"],
        closing_accounts=[
            r for r in aq if r["kind"] in {"account", "snapshot"} and r["time"].endswith("15:30:00")
        ],
        unverified_jq_rules=result["execution_policy"]["unverified_jq_rules"],
        strict_equivalence=False,
        historical_pit_verified=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ad-export", type=Path, required=True)
    parser.add_argument("--inherited-input", type=Path, required=True)
    parser.add_argument("--jq", type=Path, required=True)
    parser.add_argument("--gap-reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing", type=Path)
    args = parser.parse_args()
    require(
        json.loads(args.input.read_bytes()) == project(args.ad_export, args.inherited_input),
        "Full input differs from fixed AD projection",
    )
    args.output.mkdir(parents=True, exist_ok=False)
    report = dict(
        schema="aq-jq-real-full-session.v1",
        ad_commit=AD_COMMIT,
        ad_export_sha256=AD_EXPORT_SHA256,
        ad_wheel_sha256=AD_WHEEL_SHA256,
        input_sha256=sha(args.input),
        cases={},
        new_platform_runs=0,
    )
    for name in CASES:
        script = args.output / (name + ".py")
        script.write_text(
            generate(name, "aq")
            if name in NAMES
            else (ROOT / "tests/fixtures/minute_reference/gap_ported.py").read_text()
        )
        root = (args.existing or args.output) / name
        if not args.existing:
            execute(script, args.input, root, config(name))
        require(sha(root / "input.json") == report["input_sha256"], "Run input changed")
        require(
            json.loads((root / "manifest.json").read_bytes())["config"] == config(name),
            "Run config changed",
        )
        report["cases"][name] = compare_case(name, root, args.jq, args.gap_reference)
        print(
            json.dumps(
                {
                    "case": name,
                    "trades": report["cases"][name]["trades"],
                    "final": report["cases"][name]["aq_final"]["total_value"],
                }
            ),
            flush=True,
        )
    (args.output / "comparison.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
