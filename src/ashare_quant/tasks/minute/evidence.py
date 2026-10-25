"""Verify incremental inputs without accumulating their records in memory."""

import json

from ashare_quant.storage.artifacts import regular, require
from ashare_quant.storage.streams import file_hash, reading


def verify_demand(root):
    doc = json.loads(regular(root / "input.json"))
    if doc.get("schema") != "aq-python-demand-input.v3":
        return  # Prior immutable formats remain readable.
    seals = doc["evidence"]
    require({item["file"] for item in seals} ==
            {"reads.jsonl.gz", "queries.jsonl.gz", "corporate.jsonl.gz"}, "PYTHON_INPUT_CHANGED")
    for item in seals:
        path = root / item["file"]
        require(file_hash(path) == item["sha256"], "PYTHON_INPUT_CHANGED")
        count = 0
        with reading(path, text=True) as stream:
            for line in stream:
                value = json.loads(line)
                count += 1
                if item["file"] == "reads.jsonl.gz" and value.get("api") == "execution_minutes":
                    relative = f"execution-cache/{value['security']}-{value['date']}/input.json.gz"
                    require(value["file"] == relative and ".." not in relative and "\\" not in relative,
                            "PYTHON_INPUT_CHANGED")
                    frozen = root / relative
                    require(not frozen.parent.is_symlink() and not frozen.parent.parent.is_symlink(),
                            "PYTHON_INPUT_CHANGED")
                    require(file_hash(frozen) == value["sha256"] and
                            file_hash(frozen, decompressed=True) == value["input_sha256"],
                            "PYTHON_INPUT_CHANGED")
        require(count == item["records"], "PYTHON_INPUT_CHANGED")
