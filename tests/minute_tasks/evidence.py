"""Test readers reconstruct small fixture evidence; production remains streamed."""
import json

from ashare_quant.storage.streams import reading
from ashare_quant.tasks.minute.evidence import verify_demand


def events(root):
    with reading(root / "events.jsonl.gz", text=True) as stream:
        return [json.loads(line) for line in stream]

def evidence(root):
    verify_demand(root)
    doc = json.loads((root / "input.json").read_text())
    for name in ("reads", "queries", "corporate"):
        with reading(root / (name + ".jsonl.gz"), text=True) as stream:
            doc[name] = [json.loads(line) for line in stream]
    doc["corporate_schedules"] = {r["key"]: r["schedule"] for r in doc["corporate"]}
    doc["daily"] = [row for r in doc["reads"] if r["api"] == "prices" for row in r["data"]]
    return doc
