import copy
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def minute_doc():
    return runpy.run_path(str(ROOT / "tests/fixtures/research/minute/make_fixture.py"))["fixture"]()


@pytest.fixture
def minute_session_doc(minute_doc):
    """Declared engineering observations, never used as real AD/JQ evidence."""
    doc = copy.deepcopy(minute_doc)
    doc["schema"] = "ad-minute-backtest.v2"
    obs = doc["session_observations"] = dict(
        schema="ad-minute-session-observations.v1",
        source_refs=["synthetic engineering fixture"],
        limitations=["Not a real market observation"],
        opening_prices=[],
        closing_marks=[],
        daily=[],
        history_minutes=[],
        history_sessions={},
    )
    for day in doc["days"]:
        for sid in doc["securities"]:
            bars = [r for r in doc["minute"] if r["security"] == sid and r["bar_end"][:10] == day]
            common = dict(
                security=sid,
                date=day,
                price_basis="raw",
                evidence="synthetic fixture",
                observed_at=day + "T16:00:00+08:00",
            )
            obs["opening_prices"].append(
                dict(
                    **common,
                    purpose="opening_price",
                    price=bars[0]["open"],
                    source_label=bars[0]["bar_end"],
                    available_at=day + "T09:30:00+08:00",
                )
            )
            obs["closing_marks"].append(
                dict(
                    **common,
                    purpose="valuation_only",
                    price=bars[-1]["close"],
                    source_label=bars[-1]["bar_end"],
                    available_at=day + "T15:00:00+08:00",
                )
            )
            obs["daily"].append(
                dict(
                    **common,
                    purpose="closed_daily",
                    close=bars[-1]["close"],
                    volume=str(sum(int(r["volume"]) for r in bars)),
                    volume_unit="share",
                    source_label=bars[-1]["bar_end"],
                    available_at=day + "T15:00:00+08:00",
                )
            )
    return doc


@pytest.fixture
def minute_warmup_doc(minute_session_doc):
    """Past bars remain a read-only history partition, with no execution day."""
    doc = minute_session_doc
    first, prior = doc["days"][0], doc["calendar"][0]
    obs = doc["session_observations"]
    obs["history_sessions"][prior] = [
        [a.replace(first, prior), b.replace(first, prior)] for a, b in doc["sessions"][first]
    ]
    for bar in doc["minute"]:
        if bar["bar_end"].startswith(first):
            row = dict(bar)
            for name in ("bar_start", "bar_end", "available_at"):
                row[name] = row[name].replace(first, prior)
            row["evidence"] += ":read-only-warmup"
            obs["history_minutes"].append(row)
    return doc


@pytest.fixture
def run_minute(tmp_path, minute_doc):
    pytest.importorskip(
        "rqalpha", reason="Native RQAlpha required; never replace with a test ledger"
    )
    from ashare_quant.backtests.minute import run_backtest

    def run(
        source,
        *,
        doc=None,
        config=None,
        name="run",
        timeout=120,
        accept_assumed=False,
        accept_state_replay=False,
    ):
        strategy = tmp_path / f"{name}.py"
        strategy.write_text(source)
        inputs = tmp_path / f"{name}.json"
        inputs.write_text(json.dumps(doc if doc is not None else minute_doc))
        root = tmp_path / name
        result = run_backtest(
            strategy,
            inputs,
            root,
            config=config,
            timeout=timeout,
            accept_assumed=accept_assumed,
            accept_state_replay=accept_state_replay,
        )
        events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        return result["result"], events, root

    return run
