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

PIN = "7b755d2bcd2a0dd64983770d7ce5cbc1ac509ba12f529098c8bd7b50f6991192"
WHEEL = "55a2b5d95597f65021215e728df7d677cd451d66b3bc473ba10477375631e739"
VERSION = "0.9.1.dev8"
ROLE = "independent_retrospective_query_research"

PRODUCTS = (
    ("2020-03-02", "interval-history"),
    ("2024-03-01", "interval-history"),
    ("2024-03-01", "dated-archive"),
)


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
    for day, source in PRODUCTS:
        value = view.read_index_members(
            "C04-members",
            "000300.XSHG",
            as_of_date=day,
            source=source,
            role=ROLE,
            allow_uncertified=True,
        )
        if (
            value["binding_sha256"] != PIN
            or value["role"] != ROLE
            or any(
                value[k] is not False
                for k in (
                    "historical_pit",
                    "strategy_visible",
                    "matching_feed",
                    "official_chain_verified",
                    "complete_membership_certified",
                    "historical_name_verified",
                )
            )
        ):
            raise ValueError("INDEPENDENT_OWNER_CAPABILITY_CHANGED")
        outputs[day + "-" + source] = value
    return dict(
        schema="aq.independent-members-public-read.v1",
        owner_version=VERSION,
        owner_binding_sha256=PIN,
        owner_wheel_sha256=WHEEL,
        owner_installed_files_checked=checked,
        network_disabled=True,
        reference_inputs_supplied=False,
        allow_uncertified=True,
        products=outputs,
    )


if __name__ == "__main__":
    print(
        json.dumps(read(Path(sys.argv[1]), Path(sys.argv[2])), ensure_ascii=False, allow_nan=False)
    )
