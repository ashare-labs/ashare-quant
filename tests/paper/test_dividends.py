from decimal import Decimal

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.paper.contracts import Decision, SessionConfig
from ashare_quant.paper.risk import decision_gate
from ashare_quant.paper.service import PaperService

from .fixtures import close, config, decision, queue


def test_non_adjacent_record_date_is_not_silently_reinterpreted(tmp_path):
    cfg = config("record-date-scope").model_copy(
        update={"cash_dividend_policy": "gross_zero_withholding_0900"}
    )
    value = decision(cfg, 2, (0, 0), "a" * 64)
    value["cash_dividends"] = [
        {
            "security": cfg.universe[0],
            "record_date": "2024-01-03",
            "ex_date": "2024-01-05",
            "pay_date": "2024-01-05",
            "cash_per_share": "1",
            "record_quantity": 100,
            "record_checkpoint": "a" * 64,
            "source_event": {"payment_time": None},
            "model_cash_available_at": "2024-01-05T09:00:00+08:00",
        }
    ]
    with pytest.raises(DomainError, match="PAPER_ACTION_POLICY_MISMATCH"):
        decision_gate(cfg, Decision.model_validate(value))


def dividend_case(root, fault=None):
    service = PaperService(root)
    cfg = SessionConfig.model_validate(
        {
            **config(root.name).model_dump(mode="json"),
            "cash_dividend_policy": "gross_zero_withholding_0900",
        }
    )
    sid = service.create(cfg)["id"]
    first = service.work(sid, queue(service, sid, cfg, 0, (100, 0)))
    parent = first["checkpoint_sha256"]
    reports = [first]
    for index in (1, 2):
        d = decision(cfg, index, (0, 0), parent)
        d["cash_dividends"] = [
            {
                "security": cfg.universe[0],
                "record_date": first["day"],
                "ex_date": "2024-01-04",
                "pay_date": "2024-01-05",
                "cash_per_share": "1",
                "record_quantity": 100,
                "record_checkpoint": first["checkpoint_sha256"],
                "source_event": {"kind": "synthetic_test_only", "payment_time": None},
                "model_cash_available_at": "2024-01-05T09:00:00+08:00",
            }
        ]
        # Cash-only ex reference is 9 in this synthetic example; the raw prior
        # close remains 10 on the ex date. Limits use the explicit ex reference.
        d["rows"][0].update(lower_limit="8.1", upper_limit="9.9")
        if index == 2:
            d["rows"][0].update(previous_close="9")
        intent = service.prepare(sid, d)
        c = close(intent["day"], intent["intent_id"])
        c["rows"][0].update(open="9", high="9", low="9", close="9")
        service.seal_close(sid, c)
        if fault:
            with pytest.raises(DomainError):
                service.work(sid, intent["day"], fault=fault)
            assert service.status(sid)["session"]["head"] == parent
            service.control(sid, "resume", parent, "same frozen dividend day")
        r = service.work(sid, intent["day"])
        assert service.work(sid, intent["day"]) == r
        reports.append(r)
        parent = r["checkpoint_sha256"]
    return reports


@pytest.mark.parametrize(
    "fault", ["inside_native_dividend", "event_committed:1", "after_complete_checkpoint"]
)
def test_receivable_survives_sell_and_new_process_payment_without_double_credit(tmp_path, fault):
    baseline = dividend_case(tmp_path / "baseline")
    recovered = dividend_case(tmp_path / "recovered", fault)
    for originals, retried in zip(baseline, recovered, strict=True):
        for key in ("account", "orders", "trades", "events"):
            assert originals["native"][key] == retried["native"][key]
    ex, paid = [r["native"] for r in baseline[1:]]
    assert ex["account"]["positions"]["000001.XSHE"]["quantity"] == 0
    assert Decimal(ex["account"]["positions"]["000001.XSHE"]["dividend_receivable"]) == 100
    assert Decimal(paid["account"]["total_cash"]) - Decimal(ex["account"]["total_cash"]) == 100
    assert Decimal(paid["account"]["positions"]["000001.XSHE"]["dividend_receivable"]) == 0
    assert paid["settlement_count"] == 3
    assert paid["opening_reconciliation"]["cash_delta"] == "100"
