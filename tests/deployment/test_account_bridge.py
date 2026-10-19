"""The market process can only invoke pinned, read-only retained account entrypoints."""

import hashlib
import sys

import pytest

from ashare_quant.contracts import DomainError
from ashare_quant.deployment.account_bridge import AccountReader


def fixture(tmp_path):
    entry = tmp_path / "entry.py"
    entry.write_text("import json,sys; print(json.dumps({'command':sys.argv[1:]}))")
    (tmp_path / "python").symlink_to(sys.executable)
    return entry, AccountReader(
        tmp_path,
        {
            "python": "python",
            "entry": "entry.py",
            "files": [
                {"path": "entry.py", "sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}
            ],
        },
    )


def test_retained_read_commands_and_changed_reader_refusal(tmp_path):
    entry, reader = fixture(tmp_path)
    assert reader.read() == {"command": ["accounts"]}
    assert reader.read("settled-account") == {"command": ["account", "settled-account"]}
    entry.write_text("raise SystemExit(0)")
    with pytest.raises(DomainError, match="Retained account reader differs"):
        reader.read()


@pytest.mark.parametrize("identifier", ["../account", "x; execute", "account\nnext-day"])
def test_arbitrary_commands_cannot_be_injected(tmp_path, identifier):
    _, reader = fixture(tmp_path)
    with pytest.raises(DomainError, match="Invalid registered account"):
        reader.read(identifier)
