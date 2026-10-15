"""Seal the native receipt and expose audited multi-security projections."""

import json
import os
import shutil
import uuid
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from ashare_quant.contracts import DomainError
from ashare_quant.daily_portfolio.audit import audit
from ashare_quant.daily_portfolio.inputs import digest as native_digest
from ashare_quant.m2_jobs.contracts import ResultReference
from ashare_quant.m2_jobs.files import atomic, canonical, digest, regular, require, sha, sync_dir

from .contracts import PortfolioResult
from .data import validate
from .registry import check_frozen_strategy


def names(manifest):
    return set(manifest.files) | {
        "manifest.json",
        "session.json",
        "receipt.json",
        "proof.json",
        "native.chk",
        "history.json",
        "result.json",
    }


def project(manifest, captured):
    check_frozen_strategy(manifest, captured)
    for name, expected in manifest.files.items():
        require(sha(captured[name]) == expected)
    require(
        captured["manifest.json"] == canonical(manifest)
        and captured["session.json"] == canonical(manifest.session)
        and sha(captured["session.json"]) == manifest.session_file_sha256
    )
    proof = json.loads(captured["proof.json"])
    receipt = json.loads(captured["receipt.json"])
    require(
        receipt["strategy"] == manifest.strategy.definition.algorithm,
        "PORTFOLIO_FROZEN_STRATEGY_CONFLICT",
        "Native receipt does not belong to the requested algorithm",
    )
    details = manifest.session["details"]
    identity = native_digest(details)
    require(
        proof["schema"] == "portfolio-job-proof.v1"
        and proof["complete"]
        and proof["cursor"] == len(manifest.execution_scope.trading_dates) * 3
        and proof["native_identity"] == manifest.session["native_identity"] == identity
        and receipt["identity"] == identity
        and proof["receipt_sha256"] == digest(receipt)
        and proof["session_sha256"] == manifest.session_sha256
        and proof["session_file_sha256"] == manifest.session_file_sha256
        and proof["checkpoint_sha256"] == sha(captured["native.chk"])
    )
    header, payload = captured["native.chk"].split(b"\n", 1)
    require(json.loads(header) == {"schema": 1, "identity": identity, "sha256": sha(payload)})
    require(
        sha(json.dumps(details["source_inventory"], sort_keys=True).encode())
        == manifest.service_identity.source_sha256
    )
    data = validate(json.loads(captured["input.json"]), manifest.preflight.selection)
    report = audit(receipt, data, json.loads(captured["execution.json"]))
    require(
        report["passed"],
        "PORTFOLIO_AUDIT_FAILED",
        "Native result failed its accounting and contract audit",
    )
    with localcontext(prec=50):
        initial = Decimal(manifest.request.initial_cash)
        final = receipt["final_account"]
        change = Decimal(final["equity"]) - initial
        peak, drawdown = initial, Decimal(0)
        daily_fees = {}
        for trade in receipt["trades"]:
            daily_fees[trade["date"]] = (
                daily_fees.get(trade["date"], Decimal(0))
                + Decimal(trade["commission"])
                + Decimal(trade["tax"])
            )
        days, cumulative_fee = [], Decimal(0)
        for day in receipt["accounts"]:
            equity = Decimal(day["equity"])
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
            fee = daily_fees.get(day["date"], Decimal(0))
            cumulative_fee += fee
            days.append(
                {
                    **day,
                    "at": day["date"] + "T15:30:00+08:00",
                    "day_fee": str(fee),
                    "cumulative_fee": str(cumulative_fee),
                }
            )
        summary = {
            "initial_cash": str(initial),
            "final_cash": final["cash"],
            "final_equity": final["equity"],
            "net_change": str(change),
            "net_return": str(change / initial),
            "max_drawdown": str(drawdown),
            "commission_total": report["commission"],
            "tax_total": report["tax"],
            "fee_total": str(Decimal(report["commission"]) + Decimal(report["tax"])),
            "trade_count": len(receipt["trades"]),
            "order_count": len(receipt["orders"]),
            "benchmark": None,
            "annualized_return": None,
            "sharpe": None,
            "metric_scope": manifest.preflight.economic_scope,
        }

    def market_time(value):
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai"))

    first_order_time = {}
    for event in receipt["order_events"]:
        first_order_time.setdefault(event["order"]["id"], market_time(event["at"]))
    trades = [{**trade, "at": market_time(trade["at"])} for trade in receipt["trades"]]
    orders = [{**order, "at": first_order_time.get(order["id"])} for order in receipt["orders"]]
    return PortfolioResult(
        job_id=manifest.job_id,
        account_id="bt-" + manifest.session["run_id"],
        request=manifest.request,
        strategy=manifest.strategy,
        scope=manifest.execution_scope,
        service_identity=manifest.service_identity,
        data_identity=manifest.data_identity,
        summary=summary,
        account=final,
        days=days,
        trades=trades,
        orders=orders,
        decisions=receipt["decisions"],
        receipt=receipt,
        audit=report,
        recovery={
            "final_cursor": proof["cursor"],
            "checkpoint_complete": True,
            "control_events": json.loads(captured["history.json"]),
        },
        assumptions=manifest.preflight.assumptions,
        limitations=manifest.preflight.limitations,
        economic_scope=manifest.preflight.economic_scope,
    )


def publish(root, manifest, invocation, attempt, history):
    captured = {name: regular(root / name) for name in manifest.files}
    captured.update(
        {
            "manifest.json": canonical(manifest),
            "session.json": regular(root / "session.json"),
            "receipt.json": regular(root / (invocation + ".receipt.json")),
            "proof.json": regular(root / (invocation + ".proof.json")),
            "native.chk": regular(root / "native.chk"),
            "history.json": canonical(history),
        }
    )
    result = project(manifest, captured)
    captured["result.json"] = canonical(result)
    seal = {
        "schema": "portfolio-publication.v1",
        "job_id": manifest.job_id,
        "manifest_sha256": digest(manifest),
        "files": {name: {"sha256": sha(raw), "size": len(raw)} for name, raw in captured.items()},
    }
    name = "publication-" + str(attempt)
    target, temp = root / name, root / ("publish-" + uuid.uuid4().hex + ".pending")
    require(not target.exists(), "M2_JOB_PUBLICATION_EXISTS")
    temp.mkdir(mode=0o700)
    try:
        for filename, raw in captured.items():
            (temp / filename).parent.mkdir(parents=True, exist_ok=True)
            atomic(temp / filename, raw)
        atomic(temp / "seal.json", seal)
        os.replace(temp, target)
        sync_dir(root)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return ResultReference(
        result_id=manifest.job_id,
        href="/api/v1/portfolio/jobs/" + manifest.job_id + "/result",
        snapshot_sha256=digest(result),
        evidence_manifest_sha256=digest(seal),
        publication=name,
    )


def read(job_dir, manifest, reference):
    try:
        root = job_dir / reference.publication
        require(not root.is_symlink())
        seal_raw = regular(root / "seal.json")
        require(sha(seal_raw) == reference.evidence_manifest_sha256)
        seal = json.loads(seal_raw)
        require(
            seal["schema"] == "portfolio-publication.v1"
            and seal["job_id"] == manifest.job_id
            and seal["manifest_sha256"] == digest(manifest)
            and set(seal["files"]) == names(manifest)
        )
        captured = {}
        for name, pin in seal["files"].items():
            require(not (root / name).parent.is_symlink())
            raw = regular(root / name)
            require(sha(raw) == pin["sha256"] and len(raw) == pin["size"])
            captured[name] = raw
        result = project(manifest, captured)
        require(
            canonical(result) == captured["result.json"]
            and digest(result) == reference.snapshot_sha256
        )
        require(regular(root / "seal.json") == seal_raw and not root.is_symlink())
        for name, raw in captured.items():
            require(regular(root / name) == raw)
        return result
    except (OSError, ValueError, KeyError, TypeError, DomainError) as exc:
        raise DomainError(
            "PORTFOLIO_RESULT_CONFLICT",
            "Published result evidence changed; discard prior success",
            409,
        ) from exc
