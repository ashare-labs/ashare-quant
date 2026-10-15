"""Sealed response regression. Collector, admission and RQAlpha remain production code.

Never imports a live account checkpoint. Every account starts inside a fresh output
directory, binds the replay corpus to its identity and is labelled non-market.
"""

import json
import sys
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from ashare_data import daily_observation as daily
from ashare_data import intraday_observation as owner
from ashare_data.intraday_refresh import QUOTE_SOURCES
from ashare_data.model import timestamp
from ashare_data.response_replay import replay

from ashare_quant.m2_jobs.files import canonical, digest, sha

from . import intraday, intraday_continuation, online
from .intraday_contracts import IntradayPlan
from .online import OnlineService
from .risk import require
from .service import engine_pin


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def load(path):
    return json.loads(Path(path).read_bytes())


def seal(source, output, recipe):
    """Copy exact selected bytes, recursively including daily comparison evidence."""
    inventory, events = {}, {}
    output.mkdir()

    def keep(relative, destination=None, expected=None):
        src = source / relative
        require(src.is_file() and not src.is_symlink(), "REPLAY_REGULAR_INPUT", relative)
        raw = src.read_bytes()
        h = sha(raw)
        require(expected is None or h == expected, "REPLAY_ORIGINAL_HASH", relative)
        target = destination or "original/" + relative
        p = output / target
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            require(p.read_bytes() == raw, "REPLAY_ARCHIVE_CONFLICT", target)
        else:
            p.write_bytes(raw)
        inventory[target] = dict(sha256=h, origin=relative, bytes=len(raw))
        return json.loads(raw) if target.endswith((".json", ".stdout")) else raw

    def event_for(name, store, ident, failed=False):
        relative = f"{store}/{'attempts' if failed else 'packets'}/{ident}.json"
        record = keep(relative, expected=None if failed else ident)
        if failed:
            command = keep(recipe["failed_command"])
            argv = command["argv"]
            after = argv[argv.index("--not-before") + 1]
            # No packet was sealed before the bug. Do not invent a completion
            # timestamp: use the observed attempt finish and label its basis.
            end, basis = record["finished_at"], "attempt_finished_at; packet absent"
        else:
            after, end, basis = (
                record["not_before"],
                record["completed_at"],
                "sealed_packet.completed_at",
            )
        responses = []
        for seq, (key, value) in enumerate(
            sorted(record["sources"].items(), key=lambda pair: timestamp(pair[1]["started_at"]))
        ):
            responses.append(dict(value, name=key, sequence=seq))
            if "sha256" in value:
                keep(
                    f"{store}/blobs/{value['sha256']}", "blobs/" + value["sha256"], value["sha256"]
                )
        event = dict(
            name=name,
            record_path="original/" + relative,
            record_sha256=inventory["original/" + relative]["sha256"],
            day=record["day"],
            kind=record["kind"],
            started_at=record["started_at"],
            completed_at=end,
            completion_basis=basis,
            not_before=after,
            responses=responses,
        )
        events[name] = event

    for name, store, ident in recipe["receipts"]:
        event_for(name, store, ident)
    event_for(*recipe["failed_attempt"], failed=True)
    daily_seen = set()

    def daily_for(cap):
        if cap in daily_seen:
            return
        daily_seen.add(cap)
        record = keep(f"observation/ad/packets/{cap}.json", f"daily/packets/{cap}.json", cap)
        attempt = record["attempt_id"]
        keep(f"observation/ad/attempts/{attempt}.json", f"daily/attempts/{attempt}.json")
        if "comparison_sha256" in record:
            keep(
                f"observation/ad/comparisons/{attempt}.json",
                f"daily/comparisons/{attempt}.json",
                record["comparison_sha256"],
            )
        for s in [
            record["calendar"]["source"],
            *[r["source"] for r in record["securities"].values()],
        ]:
            keep("observation/ad/blobs/" + s["sha256"], "daily/blobs/" + s["sha256"], s["sha256"])
        for prior in record.get("comparison_captures", []):
            daily_for(prior)

    plans = [keep(p) for p in recipe["plans"]]
    for plan in plans:
        daily_for(plan["daily_capture"])
    for key in ("prepare14", "prepare15", "opening14", "closing14", "opening15"):
        keep(recipe[key])
    for path in ("ACTIVE_PAPER_RUNTIME.json", "CLOSE_NEXTDAY_HANDOFF.json"):
        keep(path)
    manifest = dict(
        schema="sealed-calendar-responses.v1",
        market_result=False,
        recipe=recipe,
        events=events,
        files=inventory,
    )
    dump(output / "manifest.json", manifest)
    return manifest


def verify_corpus(root):
    manifest = load(root / "manifest.json")
    require(manifest["schema"] == "sealed-calendar-responses.v1", "REPLAY_CORPUS_SCHEMA")
    for name, entry in manifest["files"].items():
        p = (root / name).resolve()
        require(p.is_relative_to(root.resolve()) and p.is_file(), "REPLAY_CORPUS_PATH")
        require(sha(p.read_bytes()) == entry["sha256"], "REPLAY_CORPUS_CHANGED", name)
    for event in manifest["events"].values():
        require(
            sha((root / event["record_path"]).read_bytes()) == event["record_sha256"],
            "REPLAY_RECORD_CHANGED",
        )
        names = list(owner.specs(event["day"], event["kind"]))
        require([e["name"] for e in event["responses"]] == names, "REPLAY_ORIGINAL_ORDER")
        record = load(root / event["record_path"])
        require(
            all(event[key] == record[key] for key in ("kind", "day", "started_at")),
            "REPLAY_EVENT_CLOCK",
        )
        if "assessment" in record:
            require(
                event["completed_at"] == record["completed_at"]
                and event["not_before"] == record["not_before"]
                and event["completion_basis"] == "sealed_packet.completed_at",
                "REPLAY_EVENT_CLOCK",
            )
        else:
            command = load(root / "original" / manifest["recipe"]["failed_command"])
            argv = command["argv"]
            require(
                event["completed_at"] == record["finished_at"]
                and event["not_before"] == argv[argv.index("--not-before") + 1]
                and event["completion_basis"] == "attempt_finished_at; packet absent",
                "REPLAY_EVENT_CLOCK",
            )
        for index, item in enumerate(event["responses"]):
            require(
                item == dict(record["sources"][item["name"]], name=item["name"], sequence=index),
                "REPLAY_EVENT_CHANGED",
                item["name"],
            )
    return manifest


class Clock:
    def __init__(self, at):
        self.at = timestamp(at)

    def now(self):
        return self.at

    def set(self, at):
        self.at = timestamp(at)


class ReplayReader(intraday.IntradayReader):
    """Invoke the actual owner readers in-process with the explicit replay clock."""

    def intraday(self, config, capture):
        require(self.pin() == config.admission_reader_sha256, "PAPER_OWNER_READER_CHANGED")
        packet = owner.read(self.phase_store, capture, replay_clock=daily.now)
        require(
            config.response_replay_sha256 is not None
            and packet.get("replay", {}).get("corpus_sha256") == config.response_replay_sha256,
            "REPLAY_CORPUS_BINDING",
        )
        return packet

    def read(self, config, kind, capture):
        require(kind == "daily", "REPLAY_DAILY_ONLY")
        require(self.pin() == config.admission_reader_sha256, "PAPER_OWNER_READER_CHANGED")
        return daily.read(self.daily_store, capture)


def account_summary(report):
    native = report["native"]
    return dict(
        cash=str(Decimal(native["account"]["total_cash"]).normalize()),
        nav=str(Decimal(native["account"]["nav"]).normalize()),
        positions={s: r["quantity"] for s, r in native["account"]["positions"].items()},
        sellable={s: r["sellable"] for s, r in native["account"]["positions"].items()},
        fees=str(sum((Decimal(t["cost"]) for t in native["trades"]), Decimal(0)).normalize()),
        trades=[
            {k: t[k] for k in ("security", "quantity", "price", "cost")} for t in native["trades"]
        ],
        orders=sorted(
            [
                {k: o[k] for k in ("security", "quantity", "filled", "remaining", "status")}
                for o in native["orders"].values()
            ],
            key=lambda o: o["security"],
        ),
        settlement_count=native["settlement_count"],
        cursor=native["cursor"],
        frozen_cash=str(Decimal(native["account"]["frozen_cash"]).normalize()),
        open_orders=native["account"]["open_orders"],
    )


def baseline(event, corpus):
    original = load(corpus / event["record_path"])
    packet = dict(
        original,
        schema=owner.LEGACY_SCHEMA,
        not_before=event["not_before"],
        completed_at=event["completed_at"],
    )
    bodies = {
        s["sha256"]: (corpus / "blobs" / s["sha256"]).read_bytes()
        for s in event["responses"]
        if "sha256" in s
    }
    try:
        assessment = owner.assess(packet, bodies)
        if "assessment" in original:
            require(assessment == original["assessment"], "REPLAY_BASELINE_DIFFERED")
        return dict(
            ready=assessment["ready"],
            blockers=assessment["blockers"],
            original_assessment_equal="assessment" in original,
        )
    except Exception as exc:
        require("assessment" not in original, "REPLAY_BASELINE_UNEXPECTED_EXCEPTION", str(exc))
        require(
            type(exc).__name__ == original["error"]["code"]
            and str(exc) == original["error"]["message"],
            "REPLAY_BASELINE_ERROR_DIFFERED",
        )
        return dict(program_error=type(exc).__name__, message=str(exc))


def quote_refresh_schedule(events, names):
    """Explicitly derived scheduling of unchanged, already verified receipts.

    Retain the full first capture and the four quote receipts of later captures,
    including their original gaps. This does not assert these requests occurred
    in one actual attempt or that immediate live retries would give these bytes.
    """
    first, last = events[names[0]], events[names[-1]]
    responses, origins = [], []
    for index, name in enumerate(names):
        event = events[name]
        selected = (
            event["responses"]
            if index == 0
            else [r for r in event["responses"] if r["name"] in QUOTE_SOURCES]
        )
        for original in selected:
            responses.append(dict(original, sequence=len(responses)))
            origins.append(
                dict(
                    record_sha256=event["record_sha256"],
                    source=original["name"],
                    original_sequence=original["sequence"],
                )
            )
    return dict(
        first,
        responses=responses,
        completed_at=last["completed_at"],
        completion_basis="last original capture.completed_at; derived request schedule",
        derived_schedule=dict(
            classification="derived_response_schedule",
            market_result=False,
            original_captures=names,
            response_origins=origins,
            receipt_times_and_body_bytes_changed=False,
            actual_one_attempt_observation=False,
        ),
    )


def run(args):
    output = args.output.resolve()
    corpus = args.corpus.resolve() if args.corpus else output / "corpus"
    if args.evidence_root:
        source = args.evidence_root.resolve()
        require(
            not output.is_relative_to(source) and not source.is_relative_to(output),
            "REPLAY_OUTPUT_OVERLAP",
        )
    else:
        require(
            not output.is_relative_to(corpus) and not corpus.is_relative_to(output),
            "REPLAY_OUTPUT_OVERLAP",
        )
    require(not output.exists(), "REPLAY_FRESH_OUTPUT_REQUIRED")
    output.mkdir(parents=True)
    if args.evidence_root:
        seal(source, corpus, load(args.recipe))
    manifest = verify_corpus(corpus)
    corpus_sha = sha((corpus / "manifest.json").read_bytes())

    # Deny hidden network fallback, even if a future parser starts networking.
    def offline(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise RuntimeError("REPLAY_NETWORK_FORBIDDEN")

    sys.addaudithook(offline)
    phases = output / "phases"
    recipe, events = manifest["recipe"], manifest["events"]
    report = dict(
        schema="response-replay-report.v1",
        market_result=False,
        corpus_sha256=corpus_sha,
        aq_engine_pin=engine_pin(),
        originals={},
        cases=[],
        native={},
    )
    packets, captures = {}, {}

    def case(name, source_name, mutation=None):
        event = events[source_name]
        attempt, packet = replay(
            phases, event, corpus / "blobs", mutation=mutation, corpus_sha256=corpus_sha
        )
        item = dict(
            name=name,
            input=source_name,
            classification="derived_fault_injection" if mutation else "recorded_response_replay",
            market_result=False,
            mutation=mutation,
            capture_id=attempt["capture_id"],
            status=attempt["status"],
            error=attempt.get("error"),
            ready=packet["assessment"]["ready"] if packet else False,
            blockers=packet["assessment"]["blockers"] if packet else [],
            source_diagnostics=packet["assessment"].get("source_diagnostics", []) if packet else [],
            refresh=packet.get("refresh") if packet else None,
        )
        item["outcome"] = (
            "admitted"
            if item["ready"]
            else "invalid_receipt_timeline"
            if item["error"]
            and item["error"]["code"]
            in {"INTRADAY_SOURCE_IDENTITY", "INTRADAY_RECEIPT_ORDER", "INTRADAY_CLOCK"}
            else "program_error"
            if item["error"]
            or any(b.startswith(("KeyError:", "TypeError:")) for b in item["blockers"])
            else "source_unavailable"
            if any(d["category"] == "source_unavailable" for d in item["source_diagnostics"])
            else "source_contract_blocked"
        )
        if mutation is None:
            original = load(corpus / event["record_path"])
            require(
                packet is not None and packet["sources"] == original["sources"],
                "REPLAY_ORIGINAL_RECEIPTS_DIFFERED",
            )
        report["cases"].append(item)
        packets[name], captures[name] = packet, attempt["capture_id"]
        if mutation and mutation["operation"] == "overlapping_requests":
            # A before/after counterexample through the real v1/v2 read path.
            # This v1 packet is explicitly derived replay evidence, never live.
            legacy = load(corpus / event["record_path"])
            legacy.update(
                sources=attempt["sources"],
                evidence_kind="sealed_response_replay",
                replay=attempt["replay"],
            )
            bodies = {
                s["sha256"]: (phases / "blobs" / s["sha256"]).read_bytes()
                for s in legacy["sources"].values()
                if "sha256" in s
            }
            legacy["assessment"] = owner.assess(legacy, bodies)
            raw = canonical(legacy)
            old_id = sha(raw)
            (phases / "packets" / (old_id + ".json")).write_bytes(raw)
            old = owner.read(phases, old_id, replay_clock=lambda: timestamp(event["completed_at"]))
            require(
                old["assessment"]["ready"] and item["error"]["code"] == "INTRADAY_RECEIPT_ORDER",
                "REPLAY_ORDER_COUNTEREXAMPLE",
            )
            item["legacy_read_counterexample"] = dict(
                capture_id=old_id, accepted=True, market_result=False
            )
        return item

    # Real failures/successes first, before any fault injection.
    for name, event in events.items():
        report["originals"][name] = baseline(event, corpus)
        item = case(name, name)
        expected = name not in {
            "15-admission-01",
            "15-admission-02",
            "15-admission-03",
            "15-execution-01",
        }
        require(item["ready"] == expected and item["error"] is None, "REPLAY_REAL_REGRESSION", name)
        require(
            not any(b.startswith(("KeyError:", "TypeError:")) for b in item["blockers"]),
            "REPLAY_SECONDARY_EXCEPTION",
            name,
        )
    for name, micros in [
        ("15-admission-01", 64712),
        ("15-admission-02", 74989),
        ("15-admission-03", 108526),
    ]:
        diagnostics = packets[name]["assessment"]["source_diagnostics"]
        require(
            any(
                d.get("source_minus_receipt_microseconds") == micros
                and d["category"] == "source_time_ahead_of_receipt"
                for d in diagnostics
            ),
            "REPLAY_CLOCK_DIAGNOSTIC",
            name,
        )
    print(
        "Real receipts replayed: 14 success; 15 failures, recovery and original TypeError",
        flush=True,
    )
    try:
        owner.read(phases, captures["14-admission"])
    except Exception as exc:
        require(getattr(exc, "code", None) == "INTRADAY_SCHEMA", "REPLAY_LIVE_READER_ISOLATION")
    else:
        raise AssertionError("live reader accepted a replay packet")
    report["live_reader_rejects_replay"] = True
    dump(output / "report.json", report)

    faults = [
        ("old-quote", "14-admission", "sse_quote_600000", "old_quote_day", {"date": "20261013"}),
        ("time-unit", "14-admission", "sse_quote_600000", "milliseconds_as_hhmmss", {}),
        ("missing-field", "14-admission", "sse_quote_600000", "missing_snapshot_field", {}),
        ("partial-response", "14-admission", "sse_master_600036", "partial_list", {}),
        ("disconnect", "14-admission", "sse_suspension", "disconnect", {}),
        ("rate-limit", "14-admission", "sse_suspension", "429", {}),
        ("service-unavailable", "14-admission", "sse_suspension", "503", {}),
        ("receipt-order", "14-admission", "sse_quote_600000", "receipt_before_request", {}),
        ("request-overlap", "14-admission", "sse_master_600036", "overlapping_requests", {}),
        ("missing-minute", "14-execution", "minute_600000", "missing_minute_field", {}),
        ("fractional-shares", "14-execution", "minute_600000", "fractional_volume", {}),
        ("old-minute", "14-execution", "minute_600000", "old_minute_day", {"date": "2026-10-13"}),
        ("minute-503", "14-execution", "minute_600000", "503", {}),
        ("quote-503", "14-admission", "sina_quotes", "503", {}),
        ("volume-unit-counterexample", "14-execution", "minute_600000", "minute_volume_lots", {}),
        ("utc-equivalent", "14-admission", "sse_quote_600000", "utc_equivalent", {}),
    ]
    for name, base, source_name, operation, extra in faults:
        item = case(name, base, dict(source=source_name, operation=operation, **extra))
        require(
            item["ready"] == (name in {"volume-unit-counterexample", "utc-equivalent"}),
            "REPLAY_FAULT_EXPECTATION",
            name,
        )
        require(item["outcome"] != "program_error", "REPLAY_FAULT_PROGRAM_ERROR", name)
        if operation in {"429", "503"}:
            error = next(
                d["error"] for d in item["source_diagnostics"] if d["source"] == source_name
            )
            require(
                error
                == dict(
                    type="HTTPError",
                    http_status=int(operation),
                    message="derived replay fault: " + operation,
                ),
                "REPLAY_ERROR_FIDELITY",
            )
    # Recovery is an actual next capture, never an invented live retry.
    require(
        packets["15-admission-04"]["assessment"]["ready"]
        and packets["15-execution-02"]["assessment"]["ready"],
        "REPLAY_RECOVERY",
    )
    # Use production capture's finite refresh policy with retained real receipts.
    # These are *derived scheduling* cases, distinct from the nine actual attempts.
    for name, tails, limit, ready in [
        ("clock-refresh-exhausted", ["15-admission-03"], 1, False),
        ("clock-refresh-recovered", ["15-admission-03", "15-admission-04"], 2, True),
    ]:
        event = quote_refresh_schedule(events, ["15-admission-02", *tails])
        dump(output / "derived-inputs" / (name + ".json"), event)
        attempt, packet = replay(
            phases,
            event,
            corpus / "blobs",
            corpus_sha256=corpus_sha,
            quote_refresh_rounds=limit,
        )
        require(
            packet is not None
            and packet["assessment"]["ready"] == ready
            and len(packet["responses"]) == 114 + 4 * limit
            and packet["refresh"]["rounds"] == limit,
            "REPLAY_QUOTE_REFRESH_RESULT",
            name,
        )
        require(
            packet["refresh"]["status"] == ("ready" if ready else "retry_limit_reached"),
            "REPLAY_QUOTE_REFRESH_STATUS",
            name,
        )
        require(
            packet["sources"]
            == {
                r["name"]: {k: v for k, v in r.items() if k not in {"name", "sequence"}}
                for r in event["responses"]
            },
            "REPLAY_QUOTE_REFRESH_PROJECTION",
        )
        report["cases"].append(
            dict(
                name=name,
                input="15-admission-02",
                classification="derived_response_schedule",
                market_result=False,
                capture_id=attempt["capture_id"],
                ready=ready,
                status=attempt["status"],
                refresh=packet["refresh"],
                blockers=packet["assessment"]["blockers"],
                source_diagnostics=packet["assessment"]["source_diagnostics"],
                requests=len(packet["responses"]),
                initial_requests=114,
                repeated_full_capture_requests=114 * (limit + 1),
                original_captures=event["derived_schedule"]["original_captures"],
                original_bytes_and_receipts_preserved=True,
                actual_one_attempt_observation=False,
            )
        )
        packets[name], captures[name] = packet, attempt["capture_id"]
    # A simultaneous suspension timeout cannot be hidden by a quote refresh.
    blocked_attempt, blocked_packet = replay(
        phases,
        events["15-admission-01"],
        corpus / "blobs",
        corpus_sha256=corpus_sha,
        quote_refresh_rounds=2,
    )
    require(
        blocked_packet["refresh"]["status"] == "blocked_other_evidence"
        and len(blocked_packet["responses"]) == 114
        and blocked_attempt["status"] == "blocked",
        "REPLAY_REFRESH_MUST_NOT_HIDE_TIMEOUT",
    )
    report["clock_refresh"] = dict(
        strict_snapshot_comparison_unchanged=True,
        tolerated_future_microseconds=0,
        original_timeout_stays_blocked=blocked_attempt["capture_id"],
        source_rounding_rule_verified=False,
        source_host_clock_offset_verified=False,
        policy="strict-receipt-order-quote-refresh.v1",
    )
    print("Derived source/clock/order/units cases replayed; running native accounts", flush=True)

    def reference(key):
        value = load(corpus / "original" / recipe[key])
        return value.get("result", value)

    clock = Clock(events["14-admission"]["completed_at"])
    reader = ReplayReader(sys.executable, args.ad_source.resolve(), corpus / "daily", phases)
    plans = [
        IntradayPlan.model_validate(
            dict(load(corpus / "original" / p), admission_reader_sha256=reader.pin())
        )
        for p in recipe["plans"]
    ]
    report["owner_pin"] = reader.pin()
    report["replay_plans"] = [p.model_dump(mode="json") for p in plans]
    with ExitStack() as stack:
        for module in (intraday, online, daily, intraday_continuation):
            stack.enter_context(patch.object(module, "now", clock.now))
        service = OnlineService(output / "account")
        service.create(
            intraday.config_for(
                plans[0], "replay-only-" + corpus_sha[:16], response_replay_sha256=corpus_sha
            )
        )
        clock.set(reference("prepare14")["decision"]["received_at"])
        # Every invalid admission reaches the real adapter without changing state.
        for item in report["cases"]:
            if item["input"] != "14-admission" or item["ready"] or not item["capture_id"]:
                continue
            before = service.path.read_bytes()
            try:
                intraday.prepare(service, reader, plans[0], item["capture_id"])
            except Exception as exc:
                require(
                    getattr(exc, "code", None) == "PAPER_INTRADAY_ADMISSION_BLOCKED",
                    "REPLAY_ADAPTER_WRONG_FAILURE",
                    str(exc),
                )
                item["adapter_rejection"] = exc.code
            else:
                raise AssertionError("invalid admission reached orders")
            require(service.path.read_bytes() == before, "REPLAY_REJECTED_WRITE")
        intent = intraday.prepare(service, reader, plans[0], captures["14-admission"])
        require(
            intraday.prepare(service, reader, plans[0], captures["14-admission"]) == intent,
            "REPLAY_DUPLICATE_INTENT",
        )
        clock.set(events["14-execution"]["completed_at"])
        for name in ("missing-minute", "fractional-shares", "old-minute", "minute-503"):
            before = service.path.read_bytes()
            try:
                intraday.deliver(service, reader, plans[0], captures[name])
            except Exception as exc:
                require(
                    getattr(exc, "code", None) == "PAPER_INTRADAY_EXECUTION_BLOCKED",
                    "REPLAY_ADAPTER_WRONG_FAILURE",
                    str(exc),
                )
            else:
                raise AssertionError("invalid execution reached orders")
            require(service.path.read_bytes() == before, "REPLAY_REJECTED_WRITE")
        intraday.deliver(service, reader, plans[0], captures["14-execution"])
        first = service.advance(str(plans[0].day), "opening")
        require(
            account_summary(first) == account_summary(reference("opening14")), "REPLAY_14_ACCOUNT"
        )
        require(
            first["real_calendar_observation"] is False
            and first["native"]["evidence_kind"] == "sealed_response_replay",
            "REPLAY_LABEL",
        )
        before = service.path.read_bytes()
        checkpoint = (service.root / str(plans[0].day) / "opening.chk").read_bytes()
        intraday.deliver(service, reader, plans[0], captures["14-execution"])
        require(service.advance(str(plans[0].day), "opening") == first, "REPLAY_DUPLICATE_ORDER")
        require(
            service.path.read_bytes() == before
            and (service.root / str(plans[0].day) / "opening.chk").read_bytes() == checkpoint,
            "REPLAY_DUPLICATE_WRITE",
        )
        report["native"]["14-opening"] = account_summary(first)
        report["native"]["duplicates_preserve_bytes"] = True
        clock.set(events["14-later-execution"]["completed_at"])
        try:
            intraday.deliver(service, reader, plans[0], captures["14-later-execution"])
        except Exception as exc:
            require(
                getattr(exc, "code", None) == "PAPER_IDEMPOTENCY_CONFLICT",
                "REPLAY_LATER_MINUTE_CONFLICT",
                str(exc),
            )
            report["native"]["later_minute_refill_rejected"] = exc.code
        else:
            raise AssertionError("later minute refilled a cancelled remainder")
        require(service.path.read_bytes() == before, "REPLAY_LATER_MINUTE_WRITE")
        clock.set(reference("closing14")["native"]["day"] + "T18:15:00+08:00")
        service.deliver_observed(reader, str(plans[0].day), "closing", plans[1].daily_capture)
        closed = service.advance(str(plans[0].day), "closing")
        require(
            account_summary(closed) == account_summary(reference("closing14")), "REPLAY_14_CLOSE"
        )
        report["native"]["14-closing"] = account_summary(closed)
        pin = digest(engine_pin())
        intraday_continuation.bind(
            service, reader, plans[1], sha(service.path.read_bytes()), pin, pin
        )
        for name in ("15-admission-01", "15-admission-02", "15-admission-03"):
            clock.set(events[name]["completed_at"])
            before = service.path.read_bytes()
            try:
                intraday.prepare(service, reader, plans[1], captures[name])
            except Exception as exc:
                require(
                    getattr(exc, "code", None) == "PAPER_INTRADAY_ADMISSION_BLOCKED",
                    "REPLAY_15_WRONG_FAILURE",
                    str(exc),
                )
            else:
                raise AssertionError("15 failed admission was accepted")
            require(service.path.read_bytes() == before, "REPLAY_REJECTED_WRITE")
        clock.set(reference("prepare15")["decision"]["received_at"])
        # The recovered source selection reaches actual admission and native
        # holding-day execution; its final account must match the original.
        intraday.prepare(service, reader, plans[1], captures["clock-refresh-recovered"])
        report["clock_refresh"]["admitted_capture"] = captures["clock-refresh-recovered"]
        clock.set(events["15-execution-01"]["completed_at"])
        before = service.path.read_bytes()
        try:
            intraday.deliver(service, reader, plans[1], captures["15-execution-01"])
        except Exception as exc:
            require(
                getattr(exc, "code", None) == "PAPER_INTRADAY_EXECUTION_BLOCKED",
                "REPLAY_15_TYPEERROR",
            )
        else:
            raise AssertionError("15 invalid execution was accepted")
        require(service.path.read_bytes() == before, "REPLAY_REJECTED_WRITE")
        clock.set(events["15-execution-02"]["completed_at"])
        intraday.deliver(service, reader, plans[1], captures["15-execution-02"])
        second = service.advance(str(plans[1].day), "opening")
        require(
            account_summary(second) == account_summary(reference("opening15")), "REPLAY_15_ACCOUNT"
        )
        report["native"]["15-opening"] = account_summary(second)
        report["clock_refresh"]["native_account_matches_original"] = True
        before = service.path.read_bytes()
        intraday.deliver(service, reader, plans[1], captures["15-execution-02"])
        require(
            service.advance(str(plans[1].day), "opening") == second
            and service.path.read_bytes() == before,
            "REPLAY_15_DUPLICATE",
        )

        # Volume is shares, not lots. Same actual bytes / 100 is a separately
        # labelled counterexample: capacity becomes less than one lot, no fill.
        other = OnlineService(output / "unit-counterexample")
        other.create(
            intraday.config_for(
                plans[0], "replay-only-volume-units", response_replay_sha256=corpus_sha
            )
        )
        clock.set(reference("prepare14")["decision"]["received_at"])
        intraday.prepare(other, reader, plans[0], captures["14-admission"])
        clock.set(events["14-execution"]["completed_at"])
        intraday.deliver(other, reader, plans[0], captures["volume-unit-counterexample"])
        counter = other.advance(str(plans[0].day), "opening")
        require(
            counter["native"]["account"]["positions"]["600000.XSHG"]["quantity"] == 0,
            "REPLAY_SHARE_UNIT",
        )
        report["native"]["derived_volume_unit_counterexample"] = account_summary(counter)

        recovered = OnlineService(output / "timezone-and-crash")
        recovered.create(
            intraday.config_for(
                plans[0], "replay-only-timezone-and-crash", response_replay_sha256=corpus_sha
            )
        )
        clock.set(reference("prepare14")["decision"]["received_at"])
        intraday.prepare(recovered, reader, plans[0], captures["utc-equivalent"])
        clock.set(events["14-execution"]["completed_at"])
        intraday.deliver(recovered, reader, plans[0], captures["14-execution"])
        try:
            recovered.advance(str(plans[0].day), "opening", fault="inside_native_trade")
        except Exception as exc:
            require(
                getattr(exc, "code", None) == "PAPER_NATIVE_FAILED" and "exited -9" in str(exc),
                "REPLAY_NATIVE_CRASH",
                str(exc),
            )
        else:
            raise AssertionError("native crash injection was not exercised")
        recovered = OnlineService(recovered.root)
        recovered.resume("Derived offline regression: killed native process during a trade")
        result = recovered.advance(str(plans[0].day), "opening")
        require(account_summary(result) == account_summary(first), "REPLAY_CRASH_RECOVERY")
        report["native"]["derived_utc_and_crash_recovery"] = account_summary(result)
    verify_corpus(corpus)
    report["passed"] = True
    report["scope"] = (
        "Offline regression only; no actual account, service, publication or new collection"
    )
    dump(output / "report.json", report)
    print(
        json.dumps(
            dict(
                passed=True,
                cases=len(report["cases"]),
                corpus_sha256=corpus_sha,
                report=str(output / "report.json"),
            ),
            ensure_ascii=False,
        )
    )
    return 0
