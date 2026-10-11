"""Local CLI: one explicit case, fixed public binding and fresh native worker."""

import argparse
import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from .contracts import CASES


def worker(args):
    from .owner import PublicOwner
    from .runner import run_case
    owner = PublicOwner(args.binding, args.owner_python, args.case)
    result = run_case(args.case, owner, args.output, reservation_model=args.reservation_model,
                      b12_control=args.b12_control)
    print(json.dumps({"case_id": args.case, "native_run": result["native_run"],
                      "reentry": result["reentry"], "status": result["result"]["status"],
                      "scenario_triggered": result["result"]["scenario_triggered"],
                      "output": str(Path(args.output).resolve())}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--owner-python", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reservation-model")
    parser.add_argument("--b12-control", choices=("keep_0920", "cancel_0928"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    paths = [str(Path(__file__).resolve().parents[2]), str(metadata.distribution("rqalpha").locate_file(""))]
    boot = ("import json,sys;paths,args=json.loads(sys.argv[1]);sys.path[:0]=paths;"
            "sys.argv=['execution-six',*args];from ashare_quant.execution_six.__main__ import main;main()")
    argv = [args.case, "--binding", str(Path(args.binding).resolve()), "--owner-python",
            str(Path(args.owner_python).absolute()), "--output", str(Path(args.output).resolve()), "--worker"]
    if args.reservation_model is not None:
        argv.extend(["--reservation-model", args.reservation_model])
    if args.b12_control is not None:
        argv.extend(["--b12-control", args.b12_control])
    env = {"PATH": os.defpath, "TZ": "Asia/Shanghai", "OPENBLAS_NUM_THREADS": "1",
           "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps([paths, argv])], env=env)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
