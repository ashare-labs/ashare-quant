"""Retain paired wheels and hash-locked dependencies for clean-directory recovery."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from pathlib import Path

from ashare_quant.identity import source_sha256
from ashare_quant.providers.runtime import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-wheel", type=Path, required=True)
    parser.add_argument("--backend-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extra", action="append", choices=["factor-evaluation"], default=[])
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    pin = verify()
    if hashlib.sha256(args.owner_wheel.read_bytes()).hexdigest() != pin["wheel_sha256"]:
        raise SystemExit("Owner wheel differs from the installed paired owner")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "local-dependencies").mkdir()
    (root / "wheels").mkdir()
    shutil.copy2(args.owner_wheel, root / "local-dependencies" / args.owner_wheel.name)
    shutil.copy2(args.backend_wheel, root / "wheels" / args.backend_wheel.name)
    subprocess.run(["uv", "export", "--offline", "--locked", "--no-dev", "--extra", "native",
                    "--no-emit-project", "--output-file", str(root / "runtime-native.txt"),
                    *[part for extra in args.extra for part in ("--extra", extra)]],
                   cwd=project, check=True, stdout=subprocess.DEVNULL)
    for name in ("uv.lock", "pyproject.toml", ".python-version"):
        shutil.copy2(project / name, root / name)
    shutil.copy2(Path(__file__).with_name("rebuild_runtime.py"), root / "rebuild_runtime.py")
    members = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(root.rglob("*")) if p.is_file()}
    manifest = {"schema": "ashare-runtime-bundle.v1", "python": platform.python_version(),
                "extras": ["native", *args.extra],
                "backend_source_sha256": source_sha256(),
                "owner_runtime_code_sha256": pin["runtime_code_sha256"],
                "backend_wheel": "wheels/" + args.backend_wheel.name,
                "files": members,
                "dependency_storage": "hash-locked index artifacts or caller-prepared uv cache",
                "platform_note": "Restore on a compatible Python/platform; this bundle does not include Python or third-party wheels"}
    (root / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"runtime_bundle": str(root), "files": len(members),
                      "backend_source_sha256": manifest["backend_source_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
