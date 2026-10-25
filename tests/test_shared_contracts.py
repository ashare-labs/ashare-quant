"""Keep public imports, schema spellings and frozen input encodings compatible."""

import math
from decimal import InvalidOperation

import pytest
from pydantic import TypeAdapter, ValidationError

from ashare_quant import contracts
from ashare_quant.backtests.minute.inputs import number, stamp
from ashare_quant.core.types import EvidenceSha, ResearchSecurity, Sha
from ashare_quant.diagnostics.contracts import ResearchSecurity as DiagnosticSecurity
from ashare_quant.paper.contracts import Security as PaperSecurity
from ashare_quant.portfolio.inputs import digest, require
from ashare_quant.storage import artifacts, canonical
from ashare_quant.tasks.contracts import ISODate, Security, exact_date


def test_existing_imports_preserve_distinct_wire_scopes():
    assert Security is PaperSecurity is DiagnosticSecurity is ResearchSecurity
    assert TypeAdapter(Sha).json_schema()["pattern"] == r"^[0-9a-f]{64}$"
    assert TypeAdapter(EvidenceSha).json_schema()["pattern"] == r"^[a-f0-9]{64}$"
    assert TypeAdapter(contracts.Security).validate_python("600000.XSHG") == "600000.XSHG"
    with pytest.raises(ValidationError):
        TypeAdapter(contracts.Security).validate_python("600519.XSHG")
    assert TypeAdapter(Security).validate_python("600519.XSHG") == "600519.XSHG"
    with pytest.raises(ValidationError):
        TypeAdapter(Security).validate_python("430047.XBSE")
    assert TypeAdapter(contracts.MarketSecurity).validate_python("430047.XBSE") == "430047.XBSE"
    assert exact_date("2025-01-02") == "2025-01-02"
    assert TypeAdapter(ISODate).validate_python("2025-01-02").isoformat() == "2025-01-02"
    with pytest.raises(ValidationError):
        TypeAdapter(ISODate).validate_python("2025-01-02T00:00:00Z")


def test_input_digest_and_errors_do_not_adopt_storage_semantics():
    assert digest({"中文": "数据"}) == "10d797d8e4bb94d8dd344d8537803dad1267fa3addf71d4e4ebb599be09f9678"
    assert digest({"中文": "数据"}) != artifacts.digest({"中文": "数据"})
    assert digest(math.nan) == artifacts.digest(math.nan)
    with pytest.raises(ValueError):
        canonical.digest(math.nan)
    with pytest.raises(ValueError, match="EXACT_CODE") as caught:
        require(False, "EXACT_CODE")
    assert type(caught.value) is ValueError
    with pytest.raises(ValueError, match="MINUTE_NUMBER_TYPE"):
        number(True)
    with pytest.raises(InvalidOperation):
        number("not a number")
    with pytest.raises(ValueError, match="MINUTE_NUMBER_UNDERFLOW"):
        number("1e-400")
    assert number("0", zero=True) == 0
    with pytest.raises(ValueError, match="MINUTE_AWARE_TIMESTAMP_REQUIRED"):
        stamp("2025-01-02T09:30:00")
    assert stamp("2025-01-02T01:30:00Z").isoformat() == "2025-01-02T09:30:00+08:00"
