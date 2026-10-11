"""Pinned AD public reads in its dedicated interpreter; no JQ inputs or network."""

import base64
import csv
import hashlib
import io
import json
import socket
import sys
import zipfile
from importlib import metadata
from pathlib import Path

PIN = "15861bfe56e337f001f4f6eac38601dcb6f051ab30fb652b566a6dc60a7e1313"
WHEEL = "0744095d75771405deec1df148a2c8dd949e960ef606198aeedc2824c7979a0a"
VERSION = "0.9.1.dev7"
ROLE = "independent_retrospective_query_research"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def denied(*args, **kwargs):
    raise RuntimeError("INDEPENDENT_READER_NETWORK_DENIED")


class OfflineSocket(socket.socket):
    connect = denied
    connect_ex = denied


def read(binding_path, wheel_path):
    if sha(binding_path.read_bytes()) != PIN or sha(wheel_path.read_bytes()) != WHEEL:
        raise ValueError("INDEPENDENT_OWNER_PIN_CHANGED")
    dist = metadata.distribution("ashare-data")
    if dist.version != VERSION:
        raise ValueError("INDEPENDENT_OWNER_VERSION_CHANGED")
    checked = 0
    with zipfile.ZipFile(wheel_path) as archive:
        for name in archive.namelist():
            if name.startswith("ashare_data/") and not name.endswith("/"):
                if archive.read(name) != dist.locate_file(name).read_bytes():
                    raise ValueError("INDEPENDENT_OWNER_INSTALLED_SOURCE_CHANGED:" + name)
                checked += 1
        record = next(n for n in archive.namelist() if n.endswith(".dist-info/RECORD"))
        for name, digest, size in csv.reader(io.StringIO(archive.read(record).decode())):
            if digest:
                data = archive.read(name)
                actual = (
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
                )
                if digest != "sha256=" + actual or len(data) != int(size):
                    raise ValueError("INDEPENDENT_OWNER_WHEEL_RECORD_CHANGED")
    socket.socket = OfflineSocket
    socket.create_connection = denied
    socket.getaddrinfo = denied
    from ashare_data import IndependentQueryView

    view = IndependentQueryView(binding_path, expected_binding_sha256=PIN)
    outputs = {}
    for product in ("B27-daily", "C02-fields", "C03-history", "B25-raw"):
        sources = (
            ("sina-pingan",) if product == "C03-history" else ("sina-pingan", "eastmoney-pingan")
        )
        for source in sources:
            outputs[product + "-" + source] = view.read_daily(
                product, "000001.XSHE", source=source, role=ROLE
            )
    for name, skip, fill in (
        ("filled", False, True),
        ("nan", False, False),
        ("skipped", True, True),
    ):
        outputs["B28-" + name] = view.read_paused(
            "B28-pause", "000002.XSHE", skip_paused=skip, fill_paused=fill, role=ROLE
        )
    for basis in ("2024-06-13", "2024-06-14"):
        for fq in (None, "pre", "post"):
            outputs[f"B25-{basis}-{fq}"] = view.read_adjusted(
                "B25-adjustment", "000001.XSHE", basis_date=basis, fq=fq, role=ROLE
            )
    outputs["B12-opening"] = view.read_opening("B12-opening", "000001.XSHE", role=ROLE)
    for value in outputs.values():
        if (
            value["binding_sha256"] != PIN
            or value["role"] != ROLE
            or any(
                value[k] is not False
                for k in ("historical_pit", "strategy_visible", "matching_feed")
            )
        ):
            raise ValueError("INDEPENDENT_OWNER_CAPABILITY_CHANGED")
    return dict(
        schema="aq.independent-public-read.v1",
        owner_version=VERSION,
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        owner_installed_files_checked=checked,
        network_disabled=True,
        reference_inputs_supplied=False,
        products=outputs,
    )


if __name__ == "__main__":
    print(
        json.dumps(read(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, allow_nan=False)
    )
