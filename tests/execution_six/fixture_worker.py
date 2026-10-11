"""Only tests import this hand-authored synthetic provider, never real cases."""

import argparse
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from ashare_quant.execution_six.contracts import CASES, Read
from ashare_quant.execution_six.runner import run_case


class SyntheticOwner:
    def __init__(self, case_id, variant="normal"):
        self.case, self.variant = CASES[case_id], variant

    def descriptor(self):
        return {"dataset_id": "synthetic-six-" + self.case.case_id + "-" + self.variant,
                "evidence_kind": "synthetic", "real_market_data": False,
                "quality": "test_fixture_only", "historical_pit": False,
                "fixture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def bootstrap(self, case):
        return {"security": case.security, "day": str(case.day),
                "calendar": [str(case.day)] if self.variant == "single_session" else
                [str(case.day - timedelta(days=1)), str(case.day), str(case.day + timedelta(days=1))],
                "limitations": ["entirely synthetic calendar, lifecycle, price and volume"]}

    def read(self, operation, logical_at, **parameters):
        clock = logical_at.strftime("%H:%M")
        suspended = self.case.case_id == "B11"
        last = "20.00" if clock < "09:30" else ("20.10" if clock < "10:01" else "20.20")
        price = {"last": last, "day_open": "20.05", "upper_limit": "23.00", "lower_limit": "18.00",
                 "suspended": suspended, "is_st": False}
        bar = {"at": logical_at.isoformat(), "open": "20.00", "high": "20.50", "low": "19.90",
               "close": last, "volume": 0 if suspended else 500000,
               "amount": "0" if suspended else "10000000"}
        if self.variant == "missing_bar" and operation == "bar":
            raise RuntimeError("SYNTHETIC_OWNER_BAR_UNAVAILABLE")
        if operation == "quote":
            value = price
            # B12 freezes an 11.00 limit; use a separate artificial price scale.
            if self.case.case_id == "B12":
                value = {**price, "last": "8.10", "day_open": "8.00", "upper_limit": "12.00", "lower_limit": "7.00"}
        elif operation == "lifecycle":
            value = {"listed": True, "active": True, "delisted": False, "synthetic": True}
        elif operation == "bar":
            value = bar if self.case.case_id != "B12" else {**bar, "open": "8.00", "high": "8.20", "low": "7.90", "close": "8.10"}
        elif operation == "history":
            value = {"rows": [], "coverage": "synthetic_observation_not_full_session"}
        elif operation == "capacity":
            if parameters.get("consumer") != "engine":
                raise RuntimeError("SYNTHETIC_OWNER_ENGINE_ONLY")
            value = {"volume": 23456000 if self.variant != "zero_capacity" else 0,
                     "basis": "synthetic_retrospective_whole_day"}
        elif operation == "opening_price":
            value = {"price": "8.00", "basis": "synthetic_opening_quote"}
        elif operation == "opening_volume":
            value = {"volume": 987600, "basis": "synthetic_observed_auction_volume"}
        elif operation == "auction":
            value = {"status": "available", "rows": [{"at": self.case.at("09:25").isoformat(),
                     "price": "8.00", "volume": 987600, "amount": "7900800"}]}
        elif operation == "valuation":
            value = {"close": "8.30" if self.case.case_id == "B12" else "20.30"}
        elif operation == "previous_close":
            value = {"close": "8.00" if self.case.case_id == "B12" else "20.00",
                     "adjustment": "synthetic_no_actions"}
        else:
            raise RuntimeError("SYNTHETIC_OWNER_UNSUPPORTED_" + operation)
        if self.variant == "invalid_capacity" and operation == "capacity":
            value = {"volume": True}
        if self.variant == "invalid_opening" and operation == "opening_price":
            value = {"price": None}
        if self.variant == "opening_unmarketable":
            if operation == "opening_price":
                value = {"price": "11.50", "basis": "synthetic_unmarketable_opening"}
            elif operation == "bar":
                value = {**value, "open": "11.50", "close": "11.50", "low": "11.30", "high": "11.80"}
        return Read.of(operation, logical_at, value,
                       {"evidence_kind": "synthetic", "fixture": "hand_authored_not_JQ",
                        "operation": operation, "clock": logical_at.isoformat()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id")
    parser.add_argument("root", type=Path)
    parser.add_argument("--variant", default="normal")
    parser.add_argument("--b12-control")
    args = parser.parse_args()
    result = run_case(args.case_id, SyntheticOwner(args.case_id, args.variant), args.root,
                      verification_only=True, b12_control=args.b12_control)
    print(json.dumps({"native_run": result["native_run"], "reentry": result["reentry"],
                      "status": result["result"]["status"]}))


if __name__ == "__main__":
    main()
