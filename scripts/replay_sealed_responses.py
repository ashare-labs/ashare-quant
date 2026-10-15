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
    parser.add_argument("--output", type=Path, required=True, help="Must not exist")
    parser.add_argument(
        "--recipe",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "examples/response-replay-20261014-15.json",
    )
    args = parser.parse_args()
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"), str(args.ad_source.resolve())]
    from ashare_quant.paper.response_replay import run

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
