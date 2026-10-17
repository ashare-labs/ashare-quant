"""Build the fixed public owner source and verify its wheel without changing the checkout."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / "src/ashare_quant/providers/data-runtime.json").read_text())
WHEEL_SHA = PIN["wheel_sha256"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, required=True, help="Owner Git checkout containing the pinned commit"
    )
    args = parser.parse_args()
    if PIN["commit"] is None:
        raise SystemExit("Data candidate is not finalized; generate its exact source commit and pin first")
    target = ROOT / "local-dependencies"
    target.mkdir(exist_ok=True)
    wheel = target / f"ashare_data-{PIN['version']}-py3-none-any.whl"
    if wheel.exists():
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != WHEEL_SHA:
            raise SystemExit(
                "Existing owner dependency differs; choose a clean candidate, do not overwrite it"
            )
        print(wheel)
        return
    with tempfile.TemporaryDirectory(prefix="aq-owner-build-") as temporary:
        root = Path(temporary)
        archive = root / "owner.tar"
        with archive.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(args.source), "archive", PIN["commit"]], stdout=stream, check=True
            )
        source = root / "source"
        source.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(source, filter="data")
        subprocess.run(
            ["uv", "build", "--wheel", "--no-sources", "--project", str(source), "--out-dir", str(root / "dist")],
            check=True,
        )
        raw = (root / "dist" / wheel.name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != WHEEL_SHA:
            raise SystemExit(
                "Owner build differs from fixed candidate; keep it uninstalled and review build inputs"
            )
        with wheel.open("xb") as stream:
            stream.write(raw)
    print(wheel)


if __name__ == "__main__":
    main()
