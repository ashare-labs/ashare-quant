"""Declared gaps/invalid prices keep positions in public strategy history windows."""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from test_research_contract import ACCEPT, make_document, save
from test_source_use import scoped_rows

from ashare_quant.backtests.minute import MinuteInputs
from ashare_quant.backtests.minute.api import API


@pytest.mark.parametrize("price_status", ["invalid", "conflict"])
def test_public_history_keeps_missing_times_without_padding_old_bars(tmp_path, price_status):
    sid = "601234.XSHG"
    doc = make_document(
        tmp_path, ids=(sid,), edit=lambda b, m: scoped_rows(b, m, price_status=price_status)
    )
    data = MinuteInputs(save(doc, tmp_path / "input.json"), **ACCEPT)
    events = []
    host = SimpleNamespace(
        data=data,
        at=datetime.fromisoformat("2025-07-02T09:34:00+08:00"),
        phase="handle_data",
        jq_lifecycle=False,
        emit=lambda kind, **kw: events.append((kind, kw)),
    )
    api = API(host)
    frame = api.attribute_history(sid, 2, "1m", ("close", "volume", "money"), fq=None)
    assert list(frame.index.strftime("%H:%M")) == ["09:33", "09:34"]
    assert frame.iloc[-1].isna().all()
    assert (
        frame.iloc[0].isna().all() if price_status == "invalid" else frame.iloc[0]["close"] == 10.3
    )
    reasons = {gap["reason"] for gap in frame.attrs["unavailable_minutes"]}
    assert "no_quote_in_selected_snapshot" in reasons
    assert ("confirmed_invalid_price" in reasons) == (price_status == "invalid")
    assert events[-1][1]["unavailable_minutes"] == frame.attrs["unavailable_minutes"]

    # Array, aligned multi-security and get_price views share the same window.
    array = api.get_bars(sid, 2, "1m", fields=("date", "close"), include_now=True)
    assert len(array) == 2 and pd.isna(array["close"][-1])
    assert str(array["date"][-1]).startswith("2025-07-02T09:34")
    aligned = api.history(2, "1m", "close", [sid], fq=None)
    assert aligned.index.equals(frame.index) and pd.isna(aligned[sid].iloc[-1])
    prices = api.get_price(sid, count=2, frequency="1m", fields=["close"], fq=None)
    assert prices.index.equals(frame.index) and pd.isna(prices["close"].iloc[-1])
    grouped = api.get_price([sid], count=2, frequency="1m", fields=["close"], fq=None)
    assert pd.isna(grouped["close"].iloc[-1])
    assert grouped.attrs["unavailable_minutes"] == frame.attrs["unavailable_minutes"]

    # Excluding now hides the current slot, never a future or past-inclusive one.
    prior = api.get_bars(sid, 2, "1m", fields=("date", "close"), df=True)
    assert list(prior.index.strftime("%H:%M")) == ["09:32", "09:33"]
    host.at = datetime.fromisoformat("2025-07-03T09:31:00+08:00")
    crossing = api.attribute_history(sid, 2, "1m", ["close"], fq=None)
    assert list(crossing.index.strftime("%d %H:%M")) == ["02 09:34", "03 09:31"]
    assert pd.isna(crossing["close"].iloc[0]) and crossing["close"].iloc[-1] == 10
    past = api.get_bars(
        sid, 2, "1m", fields=("date", "close"), end_dt="2025-07-02T09:34:00+08:00", df=True
    )
    assert list(past.index.strftime("%H:%M")) == ["09:33", "09:34"]
    assert pd.isna(past["close"].iloc[-1])
    # A history placeholder is not inserted into the execution bar collection.
    assert (sid, datetime.fromisoformat("2025-07-02T09:34:00+08:00")) not in data.bars
