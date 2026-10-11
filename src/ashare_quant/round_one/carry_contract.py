"""Engine-facing facts and explicit lifecycle models; no collection or fact filling."""

from datetime import date, datetime
from decimal import Decimal

import numpy as np

from ashare_quant.contracts import SHANGHAI


def dividend_records(fact, *, security: str, as_of: datetime, role: str):
    """Translate one supplied gross cash fact, without asserting other events absent."""
    if role != "engine_known_event_research":
        raise RuntimeError("DIVIDEND_ENGINE_RESEARCH_ROLE_REQUIRED")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise RuntimeError("DIVIDEND_TIMEZONE_REQUIRED")
    if (
        fact.get("schema") != "ad.document-cash-dividend-fact.v1"
        or fact.get("event_type") != "cash_dividend"
        or fact.get("currency") != "CNY"
        or fact.get("security") != security
    ):
        raise RuntimeError("DIVIDEND_FACT_SCHEMA_UNSUPPORTED")
    if fact.get("historical_pit") is not False or fact.get("strategy_visible") is not False:
        raise RuntimeError("DIVIDEND_RESEARCH_VISIBILITY_CONTRACT_REQUIRED")
    announced = date.fromisoformat(fact["announcement_date"])
    # Date-only publication never authorizes a same-day earlier clock.
    if announced >= as_of.astimezone(SHANGHAI).date():
        raise RuntimeError("DIVIDEND_PUBLICATION_TIME_NOT_YET_RESOLVED")
    record, ex, pay = [
        date.fromisoformat(fact[k]) for k in ["record_date", "ex_date", "payment_date"]
    ]
    if not record < ex <= pay:
        raise RuntimeError("DIVIDEND_DATE_ORDER_UNSUPPORTED")
    if any(Decimal(fact[k]) != 0 for k in ["bonus_share_ratio", "capitalization_share_ratio"]):
        raise RuntimeError("DIVIDEND_ADDITIONAL_SHARE_ACTION_UNSUPPORTED")
    value = fact["gross_cash_per_10_shares"]
    if type(value) is not str:
        raise RuntimeError("DIVIDEND_DECIMAL_STRING_REQUIRED")
    amount = Decimal(value)
    if not amount.is_finite() or amount <= 0:
        raise RuntimeError("DIVIDEND_AMOUNT_INVALID")

    def key(d):
        return int(d.strftime("%Y%m%d"))

    return np.array(
        [(key(record), key(announced), float(amount), key(ex), key(pay), 10.0)],
        dtype=[
            ("book_closure_date", "<i8"),
            ("announcement_date", "<i8"),
            ("dividend_cash_before_tax", "<f8"),
            ("ex_dividend_date", "<i8"),
            ("payable_date", "<i8"),
            ("round_lot", "<f8"),
        ],
    )


def require_carry_admission(coverage):
    """Unknown coverage remains a refusal, including a known single cash event."""
    missing = [
        name
        for name in ("calendar_adjacency", "action_coverage", "lifecycle")
        if coverage.get(name) is not True
    ]
    if missing:
        raise RuntimeError("CARRY_FACT_CAPABILITIES_UNKNOWN:" + ",".join(missing))


def cash_capabilities():
    return dict(
        available_to_order="native Account.cash",
        total_cash="native Account.total_cash",
        reserved_for_orders="native Account.frozen_cash",
        transferable_cash="CashAccount.transferable_cash (opt-in AQ model)",
        transferable_cash_status="IMPLEMENTED_CONDITIONAL_MODEL_NOT_JQ_CERTIFIED",
        model="aq.stock-cash-settlement.v1",
        default_existing_runs_unchanged=True,
        inference_from_available_cash=False,
        second_cash_ledger=False,
    )


def admit_dividend_execution(fact, *, security, as_of, next_session, model):
    """Require separate AQ choices before a document fact reaches native positions.

    Native RQAlpha uses the ex-date opening quantity. This is an eligible record
    quantity only for the bounded overnight case with adjacent record/ex sessions,
    no outside position changes, and no intervening account transfers.
    """
    expected = dict(
        schema="aq.cash-dividend-execution.v1",
        qualification="virtual_position_at_record_close_carried_unchanged_to_ex_open",
        tax="gross_research_no_withholding_or_deferred_tax",
        posting="native_PRE_BEFORE_TRADING_on_payment_session",
        withdrawal="credited_payment_is_settled_in_virtual_account",
        event_set="known_cash_event_only_other_categories_require_separate_coverage",
        official_intraday_time=False,
        jq_equivalence=False,
    )
    if model != expected:
        raise RuntimeError("DIVIDEND_EXPLICIT_ACCOUNT_MODEL_REQUIRED")
    records = dividend_records(
        fact, security=security, as_of=as_of, role="engine_known_event_research"
    )
    record = date.fromisoformat(fact["record_date"])
    ex = date.fromisoformat(fact["ex_date"])
    following = next_session(record)
    following = following.date() if hasattr(following, "date") else following
    if following != ex or following <= record:
        raise RuntimeError("DIVIDEND_RECORD_EX_ADJACENCY_REQUIRED")
    return records
