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

PIN = "7ce5cf798cc316127efc4ce3b350722ba4257acf4f87becb0999407c37362ba9"
WHEEL = "76c53ff8d1113483cebc7e75326c94d41f8dedd9a5f8e5e909902176a4584e4f"
VERSION = "0.9.1.dev10"
MANIFEST = "4a69a40ad20c0738f0ec54371700bd97c196c4149b7f8eba6baf421bec009847"
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
    if (
        sha((binding_path.parent / "handoff.json").read_bytes())
        != "3e14609cd13dae0266253496b86cdade3960f8e6e5be3783fae739117004ccee"
    ):
        raise ValueError("INDEPENDENT_OWNER_HANDOFF_CHANGED")
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
    outputs = {
        "b25-issuer-evidence": view.read_action_evidence(
            "B25-issuer-evidence",
            "000001.XSHE",
            role=ROLE,
        )
    }
    for label in ("early", "late"):
        outputs[f"c05-{label}-daily"] = view.read_archive_daily(
            "C05-archive-daily",
            "000001.XSHE",
            source=f"dolt-{label}-price",
            role=ROLE,
        )
        outputs[f"c05-{label}-calendar"] = view.read_archive_calendar(
            "C05-archive-calendar",
            "000001.XSHE",
            source=f"dolt-{label}-calendar",
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
        schema="aq.action-archive-public-read.v1",
        owner_version=VERSION,
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        owner_manifest_sha256=MANIFEST,
        owner_manifest_files_checked=manifest_files,
        owner_installed_files_checked=checked,
        network_disabled=True,
        reference_inputs_supplied=False,
        products=outputs,
        product_contracts=json.loads(binding_path.read_text())["products"],
    )


if __name__ == "__main__":
    print(
        json.dumps(read(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, allow_nan=False)
    )
