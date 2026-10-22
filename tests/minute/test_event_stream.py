"""Audit events stay visible and their run-scoped handle closes on every exit."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ashare_quant.backtests.minute.runner import MinuteRunError


def test_callback_reads_completed_events_before_failure(run_minute, tmp_path):
    events = tmp_path / "run/events.jsonl"
    source = f'''import json
from pathlib import Path
from jqdata import record, run_daily, order
def initialize(context):
    run_daily(buy, "09:30")
    run_daily(fail, "09:32")
def buy(context): order("600000.XSHG", 100)
def fail(context):
    record(事件即时可见=1.25)
    rows = [json.loads(s) for s in Path({str(events)!r}).read_text().splitlines()]
    assert rows[-1]["kind"] == "record"
    assert any(row["kind"] == "trade" for row in rows)
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    raise RuntimeError("visible-before-failure")
'''
    with pytest.raises(MinuteRunError, match="visible-before-failure"):
        run_minute(source)
    rows = [json.loads(s) for s in events.read_text().splitlines()]
    assert rows[-1]["kind"] == "record"
    assert not (tmp_path / "run/complete.json").exists()


def test_worker_timeout_preserves_last_completed_event(run_minute, tmp_path):
    source = '''from jqdata import record
def initialize(context):
    record(before_timeout=1)
    while True:
        pass
'''
    with pytest.raises(MinuteRunError, match="MINUTE_STRATEGY_TIMEOUT"):
        run_minute(source, timeout=5)
    rows = [json.loads(s) for s in (tmp_path / "run/events.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "record"
    assert "before_timeout" in json.dumps(rows[-1])
    assert not (tmp_path / "run/complete.json").exists()


@pytest.mark.parametrize("failure", ["none", "callback", "teardown"])
def test_run_closes_event_handle_on_success_and_failures(tmp_path, minute_doc, failure):
    pytest.importorskip("rqalpha")
    import ashare_quant

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(minute_doc))
    root = tmp_path / "run"
    root.mkdir()
    # Keep upstream's module/global changes inside a fresh process, as the public
    # runner does. Track actual file handles while exercising the native engine.
    code = '''import json, sys
from pathlib import Path
from ashare_quant.backtests.minute.inputs import MinuteInputs
from ashare_quant.backtests.minute.native import run_native, runtime
from ashare_quant.backtests.minute.runner import DEFAULTS
root, input_path, failure = sys.argv[1:]
root = Path(root)
data = MinuteInputs(input_path)
opened = []
original_open = Path.open
def tracked_open(path, *args, **kwargs):
    stream = original_open(path, *args, **kwargs)
    if path == root / "events.jsonl":
        opened.append(stream)
    return stream
Path.open = tracked_open
if failure == "teardown":
    import ashare_quant.execution.synthetic.mod as storage
    original_atomic = storage.atomic_json
    def fail_teardown(path, content):
        if path.name == "teardown.json":
            raise OSError("teardown-write-failure")
        return original_atomic(path, content)
    storage.atomic_json = fail_teardown
source = b"def initialize(context): pass"
if failure == "callback":
    source = b"def initialize(context): raise ValueError('callback-failure')"
try:
    run_native(root, data, source, DEFAULTS, "event-stream-test")
except BaseException as exc:
    error = getattr(getattr(exc, "error", None), "exc_val", exc)
    expected = {"callback": "callback-failure", "teardown": "teardown-write-failure"}
    assert failure in expected and expected[failure] in str(error)
else:
    assert failure == "none"
finally:
    Path.open = original_open
assert opened and all(stream.closed for stream in opened)
assert "event_stream" not in runtime
rows = [json.loads(s) for s in (root / "events.jsonl").read_text().splitlines()]
assert rows and [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
'''
    env = dict(os.environ, PYTHONPATH=str(Path(ashare_quant.__file__).parents[1]))
    subprocess.run([sys.executable, "-B", "-c", code, str(root), str(input_path), failure],
                   env=env, check=True, capture_output=True, text=True)
