"""A prepare/inspect/run protocol around daily_portfolio.run; no second engine."""

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from ashare_quant.identity import source_sha256
from ashare_quant.portfolio.inputs import digest as native_digest
from ashare_quant.storage.artifacts import atomic, canonical, digest, regular, require, sha


def run(arguments):
    require(
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
        "PORTFOLIO_ISOLATED_RUNNER_REQUIRED",
    )
    if arguments["action"] == "run":
        from ashare_quant.tasks.daily.runner import watch_control

        require(arguments["control"] is not None, "PORTFOLIO_CONTROL_REQUIRED")
        threading.Thread(
            target=watch_control,
            args=(arguments["control"], arguments["resource_limits"]["execution_seconds"]),
            daemon=True,
        ).start()
    from ashare_quant.execution.rqalpha.guards import install_network_guard

    install_network_guard()
    from ashare_quant.execution.rqalpha.checkpoint import AtomicFile
    from ashare_quant.portfolio.__main__ import run as native_run

    root, invocation = Path(arguments["root"]), arguments["invocation"]
    require(source_sha256() == arguments["source_sha256"], "PORTFOLIO_EXECUTOR_CHANGED")
    config = json.loads(regular(root / "execution.json"))
    request = json.loads(regular(root / "submission.json"))
    args = SimpleNamespace(
        output=root,
        manifest=root / "execution.json",
        input=root / "input.json",
        strategy=config["strategies"][0]["id"],
        window="selected",
        invocation=invocation,
        preflight=False,
        fault=arguments["fault"],
        accept_owner_state_model=request["accept_retrospective_state"],
        accept_price_only_research=request["mode"] == "price_only_no_limits",
        prepare_only=True,
    )
    details = native_run(args)
    identity = native_digest(details)
    session = {
        "run_id": root.name.removeprefix("m2job-"),
        "native_identity": identity,
        "details": details,
    }
    if arguments["action"] == "prepare":
        atomic(root / "session.json", session)
    else:
        require(
            sha(regular(root / "session.json")) == arguments["session_file_sha256"]
            and regular(root / "session.json") == canonical(session),
            "PORTFOLIO_SESSION_CHANGED",
        )
    if arguments["action"] == "run":
        args.prepare_only = False
        args.expected_native_identity = identity
        # The preparatory call wrote only a preflight; the native runner owns its
        # checkpoint identity, locking, source pinning and complete reentry.
        code = native_run(args)
        require(code == 0, "PORTFOLIO_AUDIT_FAILED", "Native accounting/contract audit failed")
    value = AtomicFile(root / "native.chk", identity).read()
    cursor = json.loads(value["states"]["event_source"])["cursor"] if value else 0
    complete = bool(value and value["complete"])
    require(0 <= cursor <= len(details["records"]), "PORTFOLIO_CURSOR_INVALID")
    if complete:
        require(cursor == len(details["records"]), "PORTFOLIO_INCOMPLETE_CHECKPOINT")
        atomic(root / (invocation + ".receipt.json"), value["receipt"])
    proof = {
        "schema": "portfolio-job-proof.v1",
        "action": arguments["action"],
        "native_identity": identity,
        "session_sha256": digest(session),
        "session_file_sha256": sha(regular(root / "session.json")),
        "cursor": cursor,
        "complete": complete,
        "checkpoint_sha256": sha(regular(root / "native.chk")) if value else None,
        "receipt_sha256": digest(value["receipt"]) if complete else None,
    }
    atomic(root / (invocation + ".proof.json"), proof)


if __name__ == "__main__":
    try:
        run(TASK_ARGUMENTS)  # noqa: F821
    except BaseException as exc:
        arguments = TASK_ARGUMENTS  # noqa: F821
        atomic(
            Path(arguments["root"]) / (arguments["invocation"] + ".runner-error.json"),
            {
                "code": getattr(exc, "code", "PORTFOLIO_EXECUTION_REFUSED"),
                "message": str(exc)[:1000],
                "type": type(exc).__name__,
            },
        )
        raise
