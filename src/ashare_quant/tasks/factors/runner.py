"""Pure offline computation in a shared-queue supervised child."""

import json
import sys
import threading
from pathlib import Path

from ashare_quant.identity import source_sha256
from ashare_quant.storage.artifacts import atomic, digest, regular, require, sha


def run(args):
    require(
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
        "FACTOR_ISOLATED_RUNNER_REQUIRED",
    )
    root = Path(args["root"])
    session = json.loads(regular(root / "session.json"))
    require(
        sha(regular(root / "session.json")) == args["session_file_sha256"]
        and source_sha256() == session["source_sha256"],
        "FACTOR_SESSION_CHANGED",
    )
    if args["action"] == "run":
        from ashare_quant.execution.rqalpha.guards import install_network_guard
        from ashare_quant.factors.compute import compute
        from ashare_quant.providers.runtime import verify
        from ashare_quant.tasks.control import watch_control
        from ashare_quant.tasks.factors.contracts import FactorCreate, FactorResult

        threading.Thread(
            target=watch_control,
            args=(args["control"], args["resource_limits"]["execution_seconds"]),
            daemon=True,
        ).start()
        install_network_guard()
        verify()
        request = FactorCreate.model_validate_json(regular(root / "submission.json"))
        frozen = json.loads(regular(root / "input.json"))
        require(
            digest(request) == session["request_sha256"]
            and digest(frozen) == session["input_sha256"],
            "FACTOR_INPUT_CHANGED",
        )
        print("Computing the selected AD factor over frozen inputs", flush=True)
        value = FactorResult.model_validate(compute(frozen, request, root.name))
        atomic(root / "result.json", value)
        atomic(root / "completed.json", {"result_sha256": digest(value)})
    complete = (root / "completed.json").exists() and json.loads(regular(root / "completed.json"))[
        "result_sha256"
    ] == digest(json.loads(regular(root / "result.json")))
    proof = dict(
        cursor=int(complete),
        complete=complete,
        checkpoint_sha256=None,
        session_sha256=digest(session),
        session_file_sha256=sha(regular(root / "session.json")),
        receipt_sha256=digest(json.loads(regular(root / "result.json"))) if complete else None,
    )
    atomic(root / (args["invocation"] + ".proof.json"), proof)


if __name__ == "__main__":
    try:
        run(TASK_ARGUMENTS)  # noqa: F821
    except BaseException as exc:
        args = TASK_ARGUMENTS  # noqa: F821
        atomic(
            Path(args["root"]) / (args["invocation"] + ".runner-error.json"),
            dict(
                code=getattr(exc, "code", "FACTOR_COMPUTE_FAILED"),
                message=str(exc)[:2000],
                details=getattr(exc, "details", None),
            ),
        )
        raise
