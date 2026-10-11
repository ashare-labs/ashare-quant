"""Synthetic extraction fixtures; real observations are separately hash checked."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "reference_mechanisms", Path(__file__).parents[2] / "tools/round_one_reference_mechanisms.py"
)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_ast_extracts_probed_attributes_without_executing_source():
    source = """
raise RuntimeError('must never execute')
CONFIG = {'close_tax': 0.1}
FEE_PROBE_FIELDS = ['transfer_fee']
TRADE_FIELDS = ['price']
def fields(obj, names):
    return getattr(obj, names[0])
def initialize():
    OrderCost(open_tax=0, close_tax=CONFIG['close_tax'])
"""
    result = MOD.source_facts(source)
    assert result["FEE_PROBE_FIELDS"] == ["transfer_fee"]
    assert result["OrderCost_call_keywords"] == [["open_tax", "close_tax"]]
    assert result["TRADE_FIELDS"] == ["price"]


def test_tampered_reference_is_refused_before_extraction(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/round-one-manifest.json").write_text("{}")
    (tmp_path / "jq.py").write_text("CONFIG = {}")
    binding = dict(
        root=str(tmp_path),
        manifest_sha256=hashlib.sha256(b"{}").hexdigest(),
        cases=[dict(case_id="B14", folder=str(tmp_path), files={"jq.py": "0" * 64})],
    )
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match="REFERENCE_FILE_HASH_MISMATCH"):
        MOD.audit(path)
