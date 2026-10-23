"""One supervised process prepares data and invokes the existing native engine."""

import json
import sys
import threading
import traceback
from pathlib import Path

from ashare_quant.storage.artifacts import atomic, digest, regular, require, sha


def run(args):
    require(
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
        "PYTHON_ISOLATED_RUNNER_REQUIRED",
    )
    root = Path(args["root"])
    session = json.loads(regular(root / "session.json"))
    require(
        sha(regular(root / "session.json")) == args["session_file_sha256"], "PYTHON_SESSION_CHANGED"
    )
    complete = False
    if args["action"] == "run":
        from ashare_quant.backtests.minute.inputs import MinuteInputs
        from ashare_quant.backtests.minute.native import run_native
        from ashare_quant.backtests.minute.runner import DEFAULTS
        from ashare_quant.execution.rqalpha.guards import install_network_guard
        from ashare_quant.tasks.control import watch_control
        from ashare_quant.tasks.minute.contracts import PythonCreate
        from ashare_quant.tasks.minute.prepare import prepare

        threading.Thread(
            target=watch_control,
            args=(args["control"], args["resource_limits"]["execution_seconds"]),
            daemon=True,
        ).start()
        install_network_guard()
        from ashare_quant.providers.runtime import verify
        from ashare_quant.runtime import runtime_fingerprint

        require(
            runtime_fingerprint(native=True) == session["runtime"]
            and digest(verify()) == session["owner_pin_sha256"],
            "PYTHON_RUNTIME_CHANGED",
        )
        request = PythonCreate.model_validate_json(regular(root / "submission.json"))
        require(digest(request) == session["request_sha256"], "PYTHON_REQUEST_CHANGED")
        atomic(root / "phase.json", {"phase": "preparing"})
        print("Preparing frozen market data", flush=True)
        config = {**DEFAULTS, "initial_cash": request.initial_cash}
        demand = session.get("data_access") == "demand.v2"
        if request.frequency == "1d":
            config.update(matching="daily_open", lifecycle="daily", capacity_policy="daily")
        if demand:
            from ashare_quant.tasks.minute.demand import DemandInputs
            from ashare_quant.tasks.policy import ServicePolicy
            data = DemandInputs(session["release"], request, root,
                                ServicePolicy.model_validate(session["data_budget"]),
                                session.get("history_factors"))
        else:
            prepare(session["release"], request, root, session.get("history_factors"))
            if request.frequency == "1d":
                from ashare_quant.backtests.daily.python_inputs import DailyPythonInputs
                data = DailyPythonInputs(root / "input.json")
            else:
                data = MinuteInputs(root / "input.json", accept_assumed=True,
                                    accept_state_replay=True, accept_research=True)
        data.validate_execution(config)
        atomic(root / "phase.json", {"phase": "executing"})
        print("Running Python strategy", flush=True)
        identity = digest(
            {
                "session": session,
                "input": session["release"] if demand else sha(regular(root / "input.json")),
                "config": config,
                "source": args["source_sha256"],
            }
        )
        result = run_native(
            root,
            data,
            regular(root / "strategy.py"),
            config,
            identity,
            parameters=request.parameters,
        )
        if demand:
            report = data.finalize()
            result["data_input_sha256"] = report["input_sha256"]
        atomic(root / "result.json", result)
        atomic(root / "completed.json", {"result_sha256": digest(result)})
        complete = True
    elif (root / "completed.json").exists():
        complete = json.loads(regular(root / "completed.json"))["result_sha256"] == digest(
            json.loads(regular(root / "result.json"))
        )
    proof = {
        "cursor": int(complete),
        "complete": complete,
        "checkpoint_sha256": None,
        "session_sha256": digest(session),
        "session_file_sha256": sha(regular(root / "session.json")),
        "receipt_sha256": digest(json.loads(regular(root / "result.json"))) if complete else None,
    }
    atomic(root / (args["invocation"] + ".proof.json"), proof)


if __name__ == "__main__":
    try:
        run(TASK_ARGUMENTS)  # noqa: F821
    except BaseException as exc:
        args = TASK_ARGUMENTS  # noqa: F821
        underlying = getattr(getattr(exc, "error", None), "exc_val", exc)
        frames = traceback.extract_tb(underlying.__traceback__)
        location = next(
            (f for f in reversed(frames) if Path(f.filename) == Path(args["root"]) / "strategy.py"),
            None,
        )
        cause_code = getattr(underlying, "code", None)
        message = f"strategy.py 第 {location.lineno} 行：" if location else ""
        message += (f"{cause_code}: " if cause_code else "") + str(underlying)
        atomic(
            Path(args["root"]) / (args["invocation"] + ".runner-error.json"),
            {
                "code": cause_code
                if str(cause_code).startswith("PYTHON_")
                else "PYTHON_RUN_FAILED",
                "cause_code": cause_code,
                "message": message[:2000],
                "details": getattr(underlying, "details", None),
                "source_line": location.lineno if location else None,
                "type": type(exc).__name__,
                "traceback": traceback.format_exc()[-6000:],
            },
        )
        raise
