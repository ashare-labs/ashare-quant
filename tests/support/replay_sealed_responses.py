#!/usr/bin/env python3
"""Single offline entry: seal existing evidence, then exercise owner + native paths."""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence-root", type=Path)
    source.add_argument("--corpus", type=Path)
    parser.add_argument("--ad-source", type=Path, required=True)
    parser.add_argument(
        "--installed-aq",
        action="store_true",
        help="Use AQ from the Python environment instead of this checkout's src (wheel validation)",
    )
    parser.add_argument("--output", type=Path, required=True, help="Must not exist")
    parser.add_argument(
        "--recipe",
        type=Path,
        required=True,
        help="Explicit external replay recipe; historical evidence is not packaged",
    )
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path[:0] = (
        [] if args.installed_aq else [str(Path(__file__).resolve().parents[2] / "src")]
    ) + [str(args.ad_source.resolve())]
    from tests.support.response_replay import run

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
