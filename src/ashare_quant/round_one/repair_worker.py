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

PIN = "fad8508ed361c9e602d92c5dcc5dae4e5e9e50badeb123759ba04dbc4674b25d"
WHEEL = "ed0eb9f6f5f29b14baf055a8825c8bc8741197733b91e680a2c9101936e30301"
VERSION = "0.9.1.dev9"
MANIFEST = "22508fd6f8fa4800dd56fe87f952ca4a3af2cbd2162f3fd7b5cd1a09283df42e"
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
    manifest_path = binding_path.parent / "MANIFEST.json"
    if sha(manifest_path.read_bytes()) != MANIFEST:
        raise ValueError("INDEPENDENT_OWNER_MANIFEST_CHANGED")
    manifest_files = 0
    for name, item in json.loads(manifest_path.read_text())["files"].items():
        path = (binding_path.parent / name).resolve()
        if not path.is_relative_to(binding_path.parent.resolve()):
            raise ValueError("INDEPENDENT_OWNER_MANIFEST_PATH_ESCAPE")
        data = path.read_bytes()
        if sha(data) != item["sha256"] or len(data) != item["bytes"]:
            raise ValueError("INDEPENDENT_OWNER_MANIFEST_FILE_CHANGED:" + name)
        manifest_files += 1
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
    for source in ("sina-pingan", "eastmoney-pingan"):
        outputs["c02-" + source] = view.read_daily(
            "C02-standardized",
            "000001.XSHE",
            source=source,
            role=ROLE,
            standardize=True,
        )
    for name, product in (
        ("c03-history", "C03-history"),
        ("c05-single-capture", "C05-single-capture"),
    ):
        outputs[name] = view.read_daily(product, "000001.XSHE", source="sina-pingan", role=ROLE)
    outputs["c03-halt-events"] = view.read_halt_events(
        "C03-halt-events",
        "000001.XSHE",
        role=ROLE,
    )
    for source in ("cninfo-dividends", "cninfo-rights"):
        outputs["b25-" + source] = view.read_corporate_actions(
            "B25-action-ledgers",
            "000001.XSHE",
            source=source,
            role=ROLE,
        )
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
        schema="aq.independent-repair-public-read.v1",
        owner_version=VERSION,
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        owner_manifest_sha256=MANIFEST,
        owner_manifest_files_checked=manifest_files,
        owner_installed_files_checked=checked,
        network_disabled=True,
        reference_inputs_supplied=False,
        standardize=True,
        products=outputs,
    )


if __name__ == "__main__":
    print(
        json.dumps(read(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, allow_nan=False)
    )
