"""Fresh-process execution, immutable inputs and checked completed reentry.

This independent experiment does not alter the existing task database or recovery
contract. Incomplete attempts are retained; arbitrary Python state is not resumed.
"""

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from ashare_quant.daily_portfolio.inputs import digest, require
from ashare_quant.execution_six.runner import atomic as atomic_json
from ashare_quant.identity import source_inventory
from ashare_quant.runtime import runtime_fingerprint

from .inputs import MinuteInputs, number

DEFAULTS = {
    "initial_cash": 100000.0,
    "volume_percent": 0.25,
    "commission_rate": 0.0003,
    "sell_tax": 0.001,
    "min_commission": 5.0,
    "matching": "next_open",
    "cash_policy": "reject",
    "lifecycle": "closed_bars",
    "capacity_policy": "strict_minute",
}
POLICIES = {
    "matching": {"next_open", "jq_current"},
    "cash_policy": {"reject", "resize"},
    "lifecycle": {"closed_bars", "jq_minute"},
    "capacity_policy": {"strict_minute", "jq_daily_initial"},
}
ARTIFACTS = (
    "manifest.json",
    "input.json",
    "strategy.py",
    "native-config.json",
    "events.jsonl",
    "native.log",
    "teardown.json",
    "result.json",
    "stdout.log",
    "stderr.log",
)


class MinuteRunError(RuntimeError):
    pass


def hashes(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ARTIFACTS}


def run_backtest(
    strategy,
    input_path,
    output,
    *,
    config=None,
    accept_assumed=False,
    accept_state_replay=False,
    accept_research=False,
    timeout=120,
):
    """Execute a trusted JQ-style script. No collection, browser or task DB access."""
    source = Path(strategy).read_bytes()
    require(len(source) <= 256 * 1024, "MINUTE_STRATEGY_TOO_LARGE")
    compile(source, str(strategy), "exec")
    require(type(timeout) in (int, float) and 0 < timeout <= 3600, "MINUTE_TIMEOUT_RANGE")
    settings = {**DEFAULTS, **(config or {})}
    require(set(settings) == set(DEFAULTS), "MINUTE_UNKNOWN_CONFIG")
    for key, value in settings.items():
        if key in POLICIES:
            require(type(value) is str and value in POLICIES[key], f"MINUTE_CONFIG_POLICY:{key}")
            continue
        require(type(value) in (int, float), "MINUTE_CONFIG_NUMERIC_TYPE")
        number(value, zero=key not in {"initial_cash", "volume_percent"})
    require(settings["volume_percent"] <= 1, "MINUTE_VOLUME_RATIO")
    data = MinuteInputs(
        input_path,
        accept_assumed=accept_assumed,
        accept_state_replay=accept_state_replay,
        accept_research=accept_research,
    )
    require(
        not (data.research and data.full_session_input)
        or settings["lifecycle"] == "jq_minute",
        "MINUTE_FULL_RESEARCH_REQUIRES_EXPLICIT_JQ_LIFECYCLE",
    )
    if settings["lifecycle"] == "jq_minute":
        from .lifecycle import validate_sessions

        validate_sessions(data)
    if settings["capacity_policy"] == "jq_daily_initial":
        require(
            settings["matching"] == "jq_current" and settings["lifecycle"] == "jq_minute",
            "JQ_DAILY_CAP_REQUIRES_JQ_MATCHING_AND_LIFECYCLE",
        )
        data.require_jq_capacity()
    from rqprobe.runtime import runtime_identity

    native_identity = runtime_identity()
    frozen = {
        "schema": "aq-python-minute-run.v1",
        "input_sha256": data.sha256,
        "strategy_sha256": hashlib.sha256(source).hexdigest(),
        "config": settings,
        "accept_assumed": accept_assumed,
        "accept_state_replay": accept_state_replay,
        "accept_research": accept_research,
        "native_runtime": native_identity,
        "runtime": runtime_fingerprint(native=True),
        "backend_sources": source_inventory(),
    }
    identity = digest(frozen)
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "writer.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root / "complete.json").exists():
            complete = json.loads((root / "complete.json").read_text())
            require(complete["identity"] == identity, "MINUTE_COMPLETED_IDENTITY_MISMATCH")
            require(complete["artifacts"] == hashes(root), "MINUTE_COMPLETED_ARTIFACT_MISMATCH")
            return {"native_run": False, "result": json.loads((root / "result.json").read_text())}
        require(
            not (root / "manifest.json").exists(), "MINUTE_INCOMPLETE_ATTEMPT_USE_NEW_DIRECTORY"
        )
        atomic_json(root / "manifest.json", {"identity": identity, **frozen})
        (root / "strategy.py").write_bytes(source)
        (root / "input.json").write_bytes(Path(input_path).read_bytes())
        sites = sorted(
            {
                str(metadata.distribution(name).locate_file(""))
                for name in ("rqalpha", "ashare-data", "pandas", "pydantic")
            }
        )
        paths = [str(Path(__file__).resolve().parents[2]), *sites]
        bootstrap = (
            "import sys,json; p,r=json.loads(sys.argv[1]);sys.path[:0]=p;"
            "from ashare_quant.minute.runner import worker;worker(r)"
        )
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
        env.update(
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            NUMEXPR_NUM_THREADS="1",
        )
        with (root / "stdout.log").open("w") as out, (root / "stderr.log").open("w") as err:
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", "-c", bootstrap, json.dumps([paths, str(root)])],
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                atomic_json(
                    root / "failure.json", {"type": "Timeout", "message": "MINUTE_STRATEGY_TIMEOUT"}
                )
                raise MinuteRunError(f"MINUTE_STRATEGY_TIMEOUT:{root}") from None
        if code != 0:
            if not (root / "failure.json").exists():
                atomic_json(
                    root / "failure.json", {"type": "WorkerExit", "message": f"exit={code}"}
                )
            failure = json.loads((root / "failure.json").read_text())
            raise MinuteRunError(f"{failure['message']}; evidence={root}")
        require((root / "result.json").is_file(), "MINUTE_RESULT_MISSING")
        atomic_json(root / "complete.json", {"identity": identity, "artifacts": hashes(root)})
        return {"native_run": True, "result": json.loads((root / "result.json").read_text())}


def worker(root):
    require(
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
        "MINUTE_ISOLATED_PROCESS_REQUIRED",
    )
    root = Path(root)
    from rqprobe.guards import install_network_guard

    install_network_guard()
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        data = MinuteInputs(
            root / "input.json",
            accept_assumed=manifest["accept_assumed"],
            accept_state_replay=manifest["accept_state_replay"],
            accept_research=manifest.get("accept_research", False),
        )
        source = (root / "strategy.py").read_bytes()
        require(
            data.sha256 == manifest["input_sha256"]
            and hashlib.sha256(source).hexdigest() == manifest["strategy_sha256"],
            "MINUTE_FROZEN_INPUT_CHANGED",
        )
        require(
            source_inventory() == manifest["backend_sources"]
            and runtime_fingerprint(native=True) == manifest["runtime"],
            "MINUTE_WORKER_RUNTIME_CHANGED",
        )
        from .native import run_native

        result = run_native(root, data, source, manifest["config"], manifest["identity"])
        atomic_json(root / "result.json", result)
    except BaseException as exc:
        error = getattr(getattr(exc, "error", None), "exc_val", exc)
        atomic_json(root / "failure.json", {"type": type(error).__name__, "message": str(error)})
        raise
