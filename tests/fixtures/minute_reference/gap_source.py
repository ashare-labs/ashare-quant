"""Export existing AD captures for the bounded gap audit; never collect new data."""

import argparse
import json
from datetime import datetime, timedelta

from tests.fixtures.minute_reference.export_boundary import PINS, verify_ad_install


def main():
    from pathlib import Path

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tdx-root", type=Path, required=True)
    p.add_argument("--ad-wheel", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    verify_ad_install(args.ad_wheel)
    from ashare_data import TDXBarView

    labels = []
    for day in ("2026-10-08", "2026-10-09", "2026-10-12"):
        for clock in ("09:30", "13:00"):
            start = datetime.fromisoformat(day + "T" + clock + ":00+08:00")
            labels += [(start + timedelta(minutes=n)).isoformat() for n in range(1, 121)]
    result = {}
    for sid, pin in PINS.items():
        read = TDXBarView(args.tdx_root / sid, expected_sha256=pin).get_price(
            mode="retrospective", allow_partial=True, labels=labels
        )
        if read.report["missing_requested_labels"]:
            raise ValueError("Missing gap-audit source labels")
        result[sid] = {
            "report": {k: v for k, v in read.report.items() if k != "capture_directory"},
            "rows": read.data.rename_axis("source_label").reset_index().to_dict("records"),
        }
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, default=str)
        stream.write("\n")


if __name__ == "__main__":
    main()
