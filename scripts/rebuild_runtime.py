"""Install a retained runtime bundle in a new environment and verify exact identity."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    target = args.destination.resolve()
    if target.exists():
        raise SystemExit("Destination must not exist; existing interpreters are never changed")
    manifest = json.loads((bundle / "runtime-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        file = (bundle / name).resolve()
        if not file.is_relative_to(bundle) or not file.is_file():
            raise SystemExit("Invalid or missing runtime member: " + name)
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise SystemExit("Runtime member differs: " + name)
    offline = ["--offline"] if args.offline else []
    subprocess.run(["uv", "venv", "--no-python-downloads", "--python", manifest["python"],
                    str(target)], check=True, cwd=bundle)
    executable = target / "bin/python"
    subprocess.run(["uv", "pip", "install", *offline, "--python", str(executable),
                    "--require-hashes", "-r", "runtime-native.txt"], check=True, cwd=bundle)
    subprocess.run(["uv", "pip", "install", *offline, "--python", str(executable),
                    "--no-deps", str(bundle / manifest["backend_wheel"])], check=True, cwd=bundle)
    code = (
        "from ashare_quant.providers.runtime import verify; "
        "from ashare_quant.runtime import runtime_fingerprint; "
        "from ashare_quant.identity import source_sha256; import json; "
        "print(json.dumps({'owner':verify()['runtime_code_sha256'],"
        "'runtime':runtime_fingerprint(native=True),'backend':source_sha256()}))"
    )
    identity = json.loads(subprocess.check_output([str(executable), "-I", "-c", code],
                                                 text=True, cwd=bundle))
    if identity["backend"] != manifest["backend_source_sha256"]:
        raise SystemExit("Rebuilt backend identity differs from retained runtime")
    if identity["owner"] != manifest["owner_runtime_code_sha256"]:
        raise SystemExit("Rebuilt owner identity differs from retained runtime")
    print(json.dumps({"environment": str(target), "verified": True, **identity}, indent=2))


if __name__ == "__main__":
    main()
