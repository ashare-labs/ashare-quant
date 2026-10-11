"""AD dev5 public reads, including the fixed standalone lifecycle research API."""

import hashlib
import importlib.util
import json
from pathlib import Path

from ashare_quant.diagnostics.runner import digest
from ashare_quant.execution_six.owner_worker import execute
from ashare_quant.execution_six.owner_worker import main as owner_main


def read(payload):
    pin = Path(__file__).with_name("carry-owner-pin.json")
    query = payload["query"]
    if query["operation"] != "carry":
        return execute(payload, pin_path=pin)
    # This also validates the installed AD runtime and installs the read-only guard.
    reply = execute({**payload, "query": {"operation": "describe"}}, pin_path=pin)
    facts = json.loads(Path(__file__).with_name("carry-data-pin.json").read_text())
    binding = Path(facts["binding"])
    if hashlib.sha256(binding.read_bytes()).hexdigest() != facts["binding_sha256"]:
        raise RuntimeError("CARRY_FACT_BINDING_CHANGED")
    reader = binding.parent / facts["reader"]["path"]
    if hashlib.sha256(reader.read_bytes()).hexdigest() != facts["reader"]["sha256"]:
        raise RuntimeError("CARRY_PUBLIC_READER_CHANGED")
    spec = importlib.util.spec_from_file_location("ad_public_carry_reader", reader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.read_carry(
        binding,
        query["window"],
        query["security"],
        role="lifecycle_research",
        expected_binding_sha256=facts["binding_sha256"],
    )
    return {**reply, "query_sha256": digest(query), "public_result": value}


def main():
    owner_main(read)
