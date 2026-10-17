"""Small publication/layout regression check; no services or account writes."""

import ast
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, text=True, capture_output=True,
    )
    if (root / "PKG-INFO").is_file():
        # An unpacked sdist has no Git metadata. Check only its declared publication inputs.
        names = [str(p.relative_to(root)) for base in ("src", "tests", "examples", "scripts", "docs")
                 for p in (root / base).rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts]
        names += [p.name for p in root.iterdir() if p.is_file()]
    elif tracked.returncode == 0:
        names = tracked.stdout.splitlines()
    else:
        raise SystemExit("Cannot inventory this checkout with git ls-files")
    paths = sorted({Path(n) for n in names if (root / n).is_file()})
    errors = []
    retired = re.compile(r"^(m2(?:_|$)|round_one$|second_batch$|execution_six$|br1_fixed$|d1(?:_|$))")
    for rel in paths:
        if rel.parts[0] in {"tools", "archives"}:
            errors.append(f"retired top-level directory: {rel}")
        if rel.parts[0] == "src" and any(retired.match(part) for part in rel.parts[1:]):
            errors.append(f"phase-based product path: {rel}")
        if rel.suffix == ".py" and not re.fullmatch(r"[a-z_][a-z_0-9]*", rel.stem):
            errors.append(f"Python filename must use snake_case: {rel}")
        if rel.suffix not in {".py", ".md", ".json", ".toml", ".txt"}:
            continue
        text = (root / rel).read_text()
        if re.search(r"/(?:Users|home)/[^/\s]+/", text):
            errors.append(f"personal absolute path: {rel}")
        if rel.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                errors.append(f"{rel}:{exc.lineno}: {exc.msg}")
                continue
            if rel.parts[0] == "src":
                for node in ast.walk(tree):
                    modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                        [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                    for module in modules:
                        if module.split(".")[0] in {"tests", "scripts", "examples"}:
                            errors.append(f"product imports development material: {rel}:{node.lineno}")
                        if re.search(r"ashare_quant\.(?:m2|round_one|second_batch|execution_six|br1_fixed|d1)(?:[._]|$)", module):
                            errors.append(f"retired product import: {rel}:{node.lineno}")
        if rel.suffix == ".md":
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = link.split("#", 1)[0].strip("<>")
                if target and not re.match(r"[a-z]+:", target) and not (root / rel.parent / target).exists():
                    errors.append(f"broken Markdown link: {rel} -> {target}")
    headings = re.findall(r"^## (.+)$", (root / "README.md").read_text(), re.M)
    if headings != ["项目简介", "安装指南", "快速开始", "使用指南", "限制与说明"]:
        errors.append("README must retain its five human-facing chapters")
    lock_raw = (root / "uv.lock").read_bytes()
    lock = tomllib.loads(lock_raw.decode())
    owner = next(p for p in lock["package"] if p["name"] == "ashare-data")
    pin = json.loads((root / "src/ashare_quant/providers/data-runtime.json").read_text())
    if owner["version"] != pin["version"] or owner["wheels"][0]["hash"] != "sha256:" + pin["wheel_sha256"]:
        errors.append("AD runtime pin and dependency lock identify different wheels")
    if (root / "src/ashare_quant/lock.sha256").read_text().strip() != hashlib.sha256(lock_raw).hexdigest():
        errors.append("Runtime lock fingerprint is stale; run scripts/freeze_runtime.py")
    identity = json.loads((root / "docs/contracts/identity.json").read_text())
    for name, expected in identity["contracts"].items():
        if hashlib.sha256((root / "docs/contracts" / name).read_bytes()).hexdigest() != expected:
            errors.append(f"Generated contract differs from identity.json: {name}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Structure, imports, naming and Markdown links checked across {len(paths)} files")


if __name__ == "__main__":
    main()
