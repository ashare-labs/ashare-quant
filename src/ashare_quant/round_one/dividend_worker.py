"""Read fixed AD facts, initialization and points; never decode or synthesize markets."""

import hashlib
import importlib.util
import json
from pathlib import Path

from ashare_quant.diagnostics.runner import digest

from .carry_worker import read as market_read


def fixed(name):
    pins = json.loads(Path(__file__).with_name("dividend-data-pin.json").read_text())
    pin = pins[name]
    path = Path(pin["path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != pin["sha256"]:
        raise RuntimeError("B26_BINDING_CHANGED:" + name)
    return path, json.loads(path.read_text())


def read(payload):
    q = payload["query"]
    if q["operation"] not in {"cash_fact", "initialization", "point"}:
        return market_read(payload)
    # Public market describe validates dev5 files/runtime and installs read-only guards.
    reply = market_read({**payload, "query": {"operation": "describe"}})
    from ashare_data import Store

    if q["operation"] == "cash_fact":
        path, binding = fixed("cash_fact")
        pins = json.loads(Path(__file__).with_name("dividend-data-pin.json").read_text())
        reader = path.parent / "read_fact.py"
        if hashlib.sha256(reader.read_bytes()).hexdigest() != pins["cash_fact_reader_sha256"]:
            raise RuntimeError("B26_FACT_READER_CHANGED")
        spec = importlib.util.spec_from_file_location("ad_public_cash_fact", reader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        value = module.read_fact(path, role="lifecycle_research")
    elif q["operation"] == "initialization":
        _, binding = fixed("queries")
        value = (
            Store(binding["store_path"])
            .jq_queries(binding["groups"]["D4"]["dataset_id"])
            .read_initialization(**q["parameters"])
            .to_dict()
        )
    else:
        _, binding = fixed("point")
        value = (
            Store(binding["owner_store"]).tdx_points(binding["dataset_id"]).read(**q["parameters"])
        )
    return {**reply, "query_sha256": digest(q), "public_result": value}


def main():
    from ashare_quant.execution_six.owner_worker import main as worker_main

    worker_main(read)
