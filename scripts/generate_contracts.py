"""Export current HTTP contracts using disposable control storage; never starts a server."""

import argparse
import hashlib
import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ashare_quant import __version__
from ashare_quant.api import create_app
from ashare_quant.identity import source_sha256
from ashare_quant.paper.api import create_app as create_paper_app
from ashare_quant.tasks.openapi import document


@contextmanager
def isolated_configuration(config_home):
    """Contract generation cannot select a developer's live deployment or daily store."""
    keys = (
        "ASHARE_DEPLOYMENT",
        "ASHARE_DATA_ROOT",
        "ASHARE_QUANT_DB",
        "ASHARE_QUANT_DAILY_STORE",
        "XDG_CONFIG_HOME",
    )
    previous = {key: os.environ.pop(key) for key in keys if key in os.environ}
    os.environ["XDG_CONFIG_HOME"] = str(config_home)
    try:
        yield
    finally:
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.update(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/contracts"))
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Create and retain isolated contract-generation storage in a new directory",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        if args.work_dir is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="aq-contracts-")))
        else:
            root = args.work_dir.resolve()
            root.mkdir(parents=True, exist_ok=False)
        stack.enter_context(isolated_configuration(root / "config"))
        app = create_app(
            str(root / "control.sqlite3"), market_stores={}, diagnostics_configuration={}
        )
        if getattr(app.state, "database_error", None):
            raise RuntimeError(app.state.database_error)
        schemas = {
            "api": app.openapi(),
            "tasks": document(app),
            "paper": create_paper_app(root / "paper").openapi(),
        }
        hashes = {}
        for name, schema in schemas.items():
            path = args.output / f"{name}.openapi.json"
            raw = (json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
            path.write_bytes(raw)
            hashes[path.name] = hashlib.sha256(raw).hexdigest()
    identity = {
        "backend_version": __version__,
        "source_sha256": source_sha256(),
        "contracts": hashes,
        "independent_review_status": "not_reviewed",
    }
    (args.output / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    print(json.dumps(identity, indent=2))


if __name__ == "__main__":
    main()
