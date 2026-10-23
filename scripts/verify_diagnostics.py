"""Reproduce the fixed three-day source-calendar diagnostic through the installed CLI."""

import argparse
import json
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--executable", type=Path, required=True)
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists():
    parser.error("Use a fresh output directory; preserve previous evidence")
args.output.mkdir(parents=True)
config = json.loads(args.config.read_text())
env = os.environ | {"ASHARE_QUANT_DIAGNOSTICS": json.dumps(config)}
database = args.output / "must-not-create.sqlite3"
base = [str(args.executable.resolve()), "--db", str(database.resolve())]
steps = []


def run(name, arguments, expected=0):
    process = subprocess.run(base + arguments, env=env, capture_output=True, text=True, timeout=45)
    (args.output / (name + ".json")).write_text(process.stdout)
    (args.output / (name + ".stderr.log")).write_text(process.stderr)
    assert process.returncode == expected, (name, process.stdout, process.stderr)
    steps.append({"name": name, "returncode": process.returncode})
    return json.loads(process.stdout)


caps = run("01-capabilities", ["source-diagnostics-capabilities"])
assert "calendar-r1" in caps["configured_sources"]
query = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/research/diagnostics/preflight-600000.json").read_text())


def diagnose(name, value, expected=0):
    request = args.output / (name + ".request.json")
    request.write_text(json.dumps(value, indent=2) + "\n")
    return run(name, ["source-diagnostics", "--request", str(request.resolve())], expected)


report = diagnose("02-readable", query)
row = report["calendar_links"]["public_result"]["rows"][0]
assert (row["previous_open"], row["trading_date"], row["next_open"]) == ("2026-09-28", "2026-09-29", "2026-09-30")
assert report["research_preflight"]["status"] == "readable"
assert report["research_values_complete"] is True
assert report["research_preflight"]["public_result"]["report_id"] == "a3ab1c866c6bbbf8027ad917cdc9cc5e80cb9379ba6143e52958f0195462a5eb"
assert not report["initialization"]["execution_permission"]
assert report["trading_facts"]["status"] == "unknown"
boundary = diagnose("03-boundary", query | {"trading_dates": ["2026-09-28"]})
assert boundary["calendar_links"]["error"]["code"] == "SOURCE_CALENDAR_BOUNDARY_UNKNOWN"
missing = diagnose("04-unconfigured", query | {"source_alias": "not-configured"}, 2)
assert missing["error"]["code"] == "DIAGNOSTIC_SOURCE_NOT_CONFIGURED"
assert not database.exists()
summary = {"passed": True, "steps": steps, "control_database_created": False,
           "execution_owner_changed": False, "market_data_created": False}
(args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary))
