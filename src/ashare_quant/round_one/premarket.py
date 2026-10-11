"""Explicit reference admission and native mark-only update; never adjust costs/cash."""

import copy
import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

from ashare_quant.execution_six.contracts import owner_price

MODEL = "aq.b26-source-premarket-reference.v1"


def collect(python, binding, wheel, security, as_of):
    result = subprocess.run(
        [
            python,
            "-I",
            "-B",
            str(Path(__file__).with_name("premarket_worker.py")),
            str(Path(binding).resolve()),
            str(Path(wheel).resolve()),
            security,
            as_of,
        ],
        cwd="/",
        env={"PATH": os.defpath, "TZ": "UTC", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("PREMARKET_PUBLIC_READ_FAILED:" + result.stderr[-4000:])
    return json.loads(result.stdout)


def admit(public, *, security, as_of, fact, raw_previous_close):
    capitalization = Decimal(str(fact.get("capitalization_share_ratio", "0")))
    if not capitalization.is_finite() or capitalization < 0:
        raise RuntimeError("PREMARKET_CAPITALIZATION_INVALID")
    policy = (
        "sina_native_prevclose_known_cash_capitalization_research.v1"
        if capitalization > 0
        else "sina_native_prevclose_known_cash_research.v1"
    )
    if (
        public.get("schema") != "ad.premarket-reference-result.v1"
        or public.get("status") != "available"
        or public.get("security") != security
        or public.get("role") != "premarket_reference_reconstruction"
        or public.get("policy") != policy
        or public.get("model_available_at") != as_of
        or public.get("historical_available_at") is not None
        or any(
            public.get(k) is not False
            for k in ("historical_pit", "strategy_visible", "matching_eligible", "current_quote")
        )
    ):
        raise RuntimeError("PREMARKET_ENVELOPE_INVALID")
    row = public["row"]
    if (
        row["ex_date"] != fact["ex_date"]
        or row["previous_session"] != fact["record_date"]
        or Decimal(row["gross_cash_per_share"]) != Decimal(fact["gross_cash_per_share"])
        or abs(Decimal(row["raw_previous_close"]) - Decimal(raw_previous_close)) > Decimal("1e-9")
        or (
            capitalization > 0
            and Decimal(row.get("capitalization_share_ratio", "-1")) != capitalization
        )
    ):
        raise RuntimeError("PREMARKET_BASIS_CONFLICT")
    return owner_price(row["reference_price"])


def apply_reference_mark(position, price):
    state = copy.deepcopy(position.get_state())
    previous = position._unadjusted_prev_close
    position.update_last_price(price)
    if position.get_state() != state or position._unadjusted_prev_close != previous:
        raise RuntimeError("PREMARKET_NATIVE_STATE_CHANGED")
