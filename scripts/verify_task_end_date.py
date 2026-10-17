"""Normal product CLI regression for an admitted two-day execution prefix."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--owner-store", type=Path, required=True)
parser.add_argument("--product", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.database.exists() or args.owner_store.exists() or args.output.exists():
    parser.error("Use fresh database, owner-store and output paths; preserve prior artifacts")
args.output.mkdir(parents=True)
examples = Path(__file__).resolve().parents[1] / "examples/python"
env = os.environ | {
    "ASHARE_QUANT_DAILY_STORE": str(args.owner_store.resolve()),
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
base = [sys.executable, "-m", "ashare_quant.cli", "--db", str(args.database.resolve())]
steps = []


def cli(name, *arguments):
    result = subprocess.run(
        base + list(arguments), env=env, capture_output=True, text=True, timeout=180
    )
    (args.output / (name + ".stdout.json")).write_text(result.stdout)
    (args.output / (name + ".stderr.log")).write_text(result.stderr)
    assert result.returncode == 0, (name, result.stdout, result.stderr)
    value = json.loads(result.stdout)
    steps.append({"name": name, "returncode": result.returncode})
    return value


cli(
    "01-register",
    "task-strategy-register-python",
    "--file",
    str(examples / "staged.py"),
    "--descriptor",
    str(examples / "descriptor.json"),
)
versions = cli("02-list-strategies", "task-python-strategies")
version = versions[0]["version_id"]
cli("03-get-strategy", "task-python-strategy-get", version)
cli("04-import", "task-data-import", "--input", str(args.product.resolve()))
catalogue = cli("05-catalogue", "task-catalogue")
window = next(w for w in catalogue["windows"] if w["scope"]["window_id"] == "w1")
request = {
    "window_id": "w1",
    "end_date": window["scope"]["trading_dates"][1],
    "name": "Owned two-day prefix",
    "idempotency_key": "owned-two-day-prefix",
    "data_binding_sha256": catalogue["data_binding_sha256"],
    "assumption_ack_sha256": window["assumption_ack_sha256"],
    "parameters": {
        "template": "registered_python.v1",
        "strategy_version_id": version,
        "initial_cash": "10000",
        "strategy_parameters": {},
    },
}
request_file = args.output / "request.json"
request_file.write_text(json.dumps(request, indent=2) + "\n")
job = cli("06-create", "task-create", "--request", str(request_file))
job_id = job["job_id"]
cli("07-status-before", "task-status", job_id)
cli("08-worker", "task-worker", "--once")
status = cli("09-status-after", "task-status", job_id)
assert status["status"] == "succeeded"
result = cli("10-result", "task-result", job_id)
assert result["summary"]["final_equity"] == "9976"
assert result["summary"]["committed_fill_count"] == 2
assert len(result["days"]) == 2
assert result["summary"]["final_cash"] == "7484"
assert result["summary"]["final_quantity"] == 200
assert result["execution_scope"]["end_date"] == request["end_date"]
assert all(d["decision_verification"] == "trusted_python_record.v1" for d in result["days"])
cli("11-manifest", "task-manifest", job_id)
cli("12-logs", "task-logs", job_id)
(args.output / "summary.json").write_text(
    json.dumps(
        {
            "steps": steps,
            "job_id": job_id,
            "version_id": version,
            "passed": True,
            "scope": "normal product CLI regression, original w1 first two admitted days",
        },
        indent=2,
    )
    + "\n"
)
print(json.dumps({"steps": len(steps), "passed": True, "job_id": job_id}))
