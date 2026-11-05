"""Verify incremental inputs without accumulating their records in memory."""

import json

from ashare_quant.storage.artifacts import regular, require
from ashare_quant.storage.streams import file_hash, reading


def verify_demand(root):
    doc = json.loads(regular(root / "input.json"))
    if doc.get("schema") != "aq-python-demand-input.v3":
        return
    seals = doc.get("evidence", [])
    require({item["file"] for item in seals} ==
            {"reads.jsonl.gz", "queries.jsonl.gz", "corporate.jsonl.gz"}, "PYTHON_INPUT_CHANGED")
