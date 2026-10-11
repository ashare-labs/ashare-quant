"""Read the pinned AD reference product from its wheel, offline and in isolation."""

import json
import socket
import sys
from hashlib import sha256
from pathlib import Path


def denied(*args, **kwargs):
    raise RuntimeError("PREMARKET_NETWORK_DENIED")


class OfflineSocket(socket.socket):
    connect = denied
    connect_ex = denied


def read(binding, wheel, security, as_of):
    pin = json.loads(Path(__file__).with_name("premarket-owner-pin.json").read_text())
    if sha256(binding.read_bytes()).hexdigest() != pin["binding_sha256"]:
        raise ValueError("PREMARKET_BINDING_CHANGED")
    if sha256(wheel.read_bytes()).hexdigest() != pin["wheel_sha256"]:
        raise ValueError("PREMARKET_AD_WHEEL_CHANGED")
    socket.socket = OfflineSocket
    socket.create_connection = socket.getaddrinfo = denied
    sys.path.insert(0, str(wheel))
    import ashare_data

    if not ashare_data.__file__.startswith(str(wheel) + "/"):
        raise ValueError("PREMARKET_AD_IMPORT_ORIGIN_CHANGED")
    result = ashare_data.PremarketReferenceView(
        binding, expected_binding_sha256=pin["binding_sha256"]
    ).read(security, as_of=as_of, role="premarket_reference_reconstruction")
    result["owner_runtime"] = dict(
        wheel_sha256=pin["wheel_sha256"],
        version=ashare_data.__version__,
        isolated=sys.flags.isolated == 1,
        network_disabled=True,
        reference_answer_inputs_supplied=False,
    )
    return result


if __name__ == "__main__":
    print(json.dumps(read(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])))
