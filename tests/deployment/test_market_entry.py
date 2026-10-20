"""The unified CLI must not drift from a fixed entry to a newer CURRENT release."""

import json

from ashare_quant.deployment import market_entry


def test_market_command_forwards_the_configured_release(tmp_path, monkeypatch):
    from ashare_data.product import configuration, release_cli

    (tmp_path / "catalog.json").write_text(json.dumps({"account_reader": {}}))
    monkeypatch.setattr(market_entry, "resolve", lambda *args: object())
    monkeypatch.setattr(
        configuration,
        "resolve",
        lambda *args: {"market_release": {"root": str(tmp_path / "market"), "release_id": 2}},
    )
    calls = []
    monkeypatch.setattr(release_cli, "main", lambda argv: calls.append(argv))
    market_entry.main(["--root", str(tmp_path), "market", "describe"])
    assert calls == [["--root", str(tmp_path / "market"), "--release-id", "2", "describe"]]
