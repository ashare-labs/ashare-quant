"""Replay only B12 original/keep/cancel against the fixed read-only AD product.

No JQ reference is consumed by the execution process. Compare saved events
afterward with b12_observable_review.py. Every run gets a fresh native account.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aq-python", required=True, type=Path)
    parser.add_argument("--owner-python", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    python = str(args.aq_python.absolute())
    site = subprocess.check_output(
        [python, "-I", "-B", "-c", "from importlib.metadata import distribution; "
         "print(distribution('rqalpha').locate_file(''))"], text=True).strip()
    boot = ("import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;"
            "sys.argv=['b12',*a];from ashare_quant.execution_six.__main__ import main;main()")
    env = {"PATH": os.defpath, "TZ": "Asia/Shanghai", "OPENBLAS_NUM_THREADS": "1",
           "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
    receipts = []
    for name in ("original", "keep_0920", "cancel_0928"):
        argv = ["B12", "--binding", str(args.binding.resolve()), "--owner-python",
                str(args.owner_python.absolute()), "--output", str(output / name), "--worker"]
        if name != "original":
            argv += ["--b12-control", name]
        command = [python, "-I", "-S", "-B", "-c", boot,
                   json.dumps([[str(ROOT / "src"), site], argv])]
        process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        (output / (name + ".stdout")).write_text(process.stdout)
        (output / (name + ".stderr")).write_text(process.stderr)
        receipts.append({"name": name, "command": command, "returncode": process.returncode})
        (output / "launch.json").write_text(json.dumps(receipts, indent=2) + "\n")
        if process.returncode:
            raise RuntimeError(f"B12 replay failed: {name}; inspect {output / (name + '.stderr')}")
        result = json.loads((output / name / "result.json").read_text())
        if not result["actual_real_case_run"] or not result["scenario_triggered"]:
            raise RuntimeError(f"B12 actual scenario not completed: {name}")
        print(json.dumps({"name": name, "status": result["status"], "identity": result["identity"]}), flush=True)


if __name__ == "__main__":
    main()
