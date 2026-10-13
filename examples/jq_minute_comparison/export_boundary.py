"""Read price-clock evidence through existing AD, exclusively from frozen local captures."""

import argparse
import json
from pathlib import Path

from ashare_quant.minute.tdx_export import verify_ad_install

PINS = {
    "600000.XSHG": "5dd97be2c2338c5327d8aad8f29df4ed48411def5b7ab4ab0e8f2e3d41d4bab5",
    "000001.XSHE": "3c1f74641eb209c5d6fcf6b42b875ab8bf63f743c21f25d8c67cf3996a7bae26",
}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tdx-root", required=True, type=Path)
    p.add_argument("--ad-wheel", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    verify_ad_install(args.ad_wheel)
    from ashare_data import TDXBarView

    labels = [
        f"{date}T{clock}:00+08:00"
        for date in ("2026-10-09", "2026-10-12")
        for clock in (
            "09:40",
            "09:41",
            "09:46",
            "09:47",
            "09:48",
            "09:49",
            "09:50",
            "09:51",
            "13:10",
            "13:11",
            "14:56",
            "15:00",
        )
    ]
    output = {}
    for sid, pin in PINS.items():
        read = TDXBarView(args.tdx_root / sid, expected_sha256=pin).get_price(
            mode="retrospective", allow_partial=True, labels=labels
        )
        if read.report["missing_requested_labels"]:
            raise ValueError("missing boundary price labels")
        output[sid] = {
            "report": {k: v for k, v in read.report.items() if k != "capture_directory"},
            "rows": read.data.rename_axis("source_label").reset_index().to_dict("records"),
        }
    # Never overwrite an earlier observation or mutate an input used for trading.
    with args.output.open("x") as f:
        json.dump(output, f, indent=2, default=str)
        f.write("\n")


if __name__ == "__main__":
    main()
