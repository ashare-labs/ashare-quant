import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def minute_doc():
    return runpy.run_path(str(ROOT / "examples/python_minute/make_fixture.py"))["fixture"]()


@pytest.fixture
def run_minute(tmp_path, minute_doc):
    pytest.importorskip(
        "rqalpha", reason="Native RQAlpha required; never replace with a test ledger"
    )
    from ashare_quant.minute import run_backtest

    def run(source, *, doc=None, config=None, name="run", timeout=120):
        strategy = tmp_path / f"{name}.py"
        strategy.write_text(source)
        inputs = tmp_path / f"{name}.json"
        inputs.write_text(json.dumps(doc if doc is not None else minute_doc))
        root = tmp_path / name
        result = run_backtest(strategy, inputs, root, config=config, timeout=timeout)
        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        return result["result"], events, root

    return run
