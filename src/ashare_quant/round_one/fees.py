"""Run one frozen single-session case in a fresh native process with the fixed AD binding."""

import argparse
import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from .cases import CASES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--owner-python", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--query-binding")
    parser.add_argument("--query-owner-python")
    parser.add_argument("--point-binding")
    parser.add_argument("--point-owner-python")
    parser.add_argument("--carry-model")
    parser.add_argument("--premarket-binding")
    parser.add_argument("--premarket-wheel")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        from .fee_runner import run_case
        from .owner import PublicOwner

        owner_kwargs = {}
        if getattr(CASES[args.case], "owner_profile", None) == "known_cash_dividend":
            from .dividend_owner import DividendOwner

            owner_type = DividendOwner
            owner_kwargs = dict(
                carry_model=args.carry_model,
                premarket_binding=args.premarket_binding,
                premarket_wheel=args.premarket_wheel,
            )
        elif getattr(CASES[args.case], "owner_profile", None) == "bounded_carry":
            from .carry_owner import CarryOwner

            owner_type = CarryOwner
            owner_kwargs = dict(carry_model=args.carry_model)
        elif getattr(CASES[args.case], "owner_profile", None) == "bounded_calendar":
            from .calendar_owner import CalendarOwner

            owner_type = CalendarOwner
        elif getattr(CASES[args.case], "owner_profile", None) == "empty_sessions":
            from .state_owner import StateOwner

            owner_type = StateOwner
        elif getattr(CASES[args.case], "owner_profile", None) == "lunch_point":
            from .point_owner import PointOwner

            if not args.point_binding or not args.point_owner_python:
                parser.error("B20 requires --point-binding and --point-owner-python")
            owner_type = PointOwner
            owner_kwargs = dict(
                point_binding=args.point_binding, point_owner_python=args.point_owner_python
            )
        elif getattr(CASES[args.case], "owner_profile", None) == "priority2":
            from .multi_owner import MultiOwner

            owner_type = MultiOwner
        else:
            owner_type = PublicOwner
        answer = run_case(
            args.case,
            owner_type(
                args.binding,
                args.owner_python,
                args.case,
                query_binding=args.query_binding,
                query_owner_python=args.query_owner_python,
                **owner_kwargs,
            ),
            args.output,
        )
        print(
            json.dumps(
                dict(
                    case_id=args.case,
                    native_run=answer["native_run"],
                    reentry=answer["reentry"],
                    status=answer["result"]["status"],
                    scenario_triggered=answer["result"]["scenario_triggered"],
                )
            )
        )
        return
    paths = [
        str(Path(__file__).resolve().parents[2]),
        str(metadata.distribution("rqalpha").locate_file("")),
    ]
    boot = "import json,sys;p,a=json.loads(sys.argv[1]);sys.path[:0]=p;sys.argv=['round-one-fees',*a];from ashare_quant.round_one.fees import main;main()"
    argv = [
        args.case,
        "--binding",
        str(Path(args.binding).resolve()),
        "--owner-python",
        str(Path(args.owner_python).absolute()),
        "--output",
        str(Path(args.output).resolve()),
        "--worker",
    ]
    if args.carry_model is not None:
        argv.extend(["--carry-model", args.carry_model])
    for flag, value in [
        ("--query-binding", args.query_binding),
        ("--query-owner-python", args.query_owner_python),
        ("--point-binding", args.point_binding),
        ("--point-owner-python", args.point_owner_python),
        ("--premarket-binding", args.premarket_binding),
        ("--premarket-wheel", args.premarket_wheel),
    ]:
        if value:
            argv.extend([flag, str(Path(value).absolute())])
    env = dict(
        PATH=os.defpath,
        TZ="Asia/Shanghai",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
    )
    raise SystemExit(
        subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps([paths, argv])], env=env
        ).returncode
    )


if __name__ == "__main__":
    main()
