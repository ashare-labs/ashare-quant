"""Synthetic market inputs only, never imported by the real runner."""

import json
import sys

from fixture_worker import SyntheticOwner

from ashare_quant.execution_six.contracts import Read
from ashare_quant.round_one.cases import CASES
from ashare_quant.round_one.fee_runner import run_case


class SyntheticQueries:
    def query(self, api, parameters, **kwargs):
        from ashare_quant.round_one.queries import QueryRefused

        if parameters.get("fields") == ["aqjq_invalid_field"]:
            raise QueryRefused("SYNTHETIC_BAD_FIELD", origin="synthetic")
        if parameters.get("end_date") == "2024-03-05":
            raise QueryRefused("SYNTHETIC_FUTURE", origin="synthetic")
        return {"rows": [{"close": "12.10"}], "evidence_kind": "synthetic"}


class FeeFixture(SyntheticOwner):
    def __init__(self, case_id, variant):
        self.case, self.variant = CASES[case_id], variant
        self.queries = SyntheticQueries()

    def read(self, operation, logical_at, **parameters):
        if self.case.case_id == "B35":
            raise RuntimeError("STATE_TEST_NO_MARKET_OR_ACTION_INPUT")
        if self.case.case_id == "B20":
            if operation == "bar":
                raise RuntimeError("SYNTHETIC_POINT_HAS_NO_OHLC")
            if operation == "point":
                if parameters.get("consumer") != "engine":
                    raise RuntimeError("SYNTHETIC_POINT_ROLE_DENIED")
                if self.variant == "missing_point":
                    raise RuntimeError("POINT_REQUIRED_OBSERVATION_UNAVAILABLE")
                return Read.of(
                    operation,
                    logical_at,
                    dict(price="20.50" if logical_at.hour == 13 else "20.30", volume=5000),
                    dict(evidence_kind="synthetic", strategy_visible=False),
                )
        read = super().read(operation, logical_at, **parameters)
        if self.case.case_id == "B31":
            value = read.value
            sid = parameters.get("security", self.case.security)
            price = "12.00" if sid == "000001.XSHE" else "8.00"
            if operation == "quote":
                value.update(last=price, day_open=price, upper_limit="15.00", lower_limit="5.00")
            elif operation == "bar":
                value.update(open=price, high=price, low=price, close=price)
            elif operation == "valuation":
                value["close"] = price
            return Read.of(
                operation, logical_at, value, {"evidence_kind": "synthetic", "security": sid}
            )
        if self.case.case_id not in {"B29", "B30", "B32"}:
            return read
        value = read.value
        if operation == "quote":
            value.update(last="12.10", day_open="12.10", upper_limit="15.00", lower_limit="10.00")
        elif operation == "bar":
            value.update(open="12.10", high="12.10", low="12.10", close="12.10")
        elif operation == "valuation":
            value["close"] = "12.30"
        return Read.of(
            operation,
            logical_at,
            value,
            {"evidence_kind": "synthetic", "fixture": "portfolio_hand_authored"},
        )

    def bootstrap(self, case):
        result = super().bootstrap(case)
        result["calendar"] = [str(case.day)]
        if self.case.case_id == "B35":
            result["calendar"] += [self.case.end_day]
            result["engine_state_scope"] = "empty_account_no_orders_or_positions"
        return result


def main():
    case, path, variant = sys.argv[1:]
    if variant in {"cash_model", "unknown_cash_model"}:
        from dataclasses import asdict, dataclass

        from ashare_quant.round_one.cash_account import MODEL
        from ashare_quant.round_one.fee_cases import FeeCase

        @dataclass(frozen=True)
        class CashCase(FeeCase):
            cash_model: str = MODEL

        CASES[case] = CashCase(
            **asdict(CASES[case]), cash_model=MODEL if variant == "cash_model" else "unknown"
        )
    answer = run_case(case, FeeFixture(case, variant), path, verification_only=True)
    print(
        json.dumps(
            dict(
                native_run=answer["native_run"],
                reentry=answer["reentry"],
                status=answer["result"]["status"],
            )
        )
    )
