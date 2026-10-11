"""AD dev3 sparse market reader, independent of the frozen old owner runtime."""

from pathlib import Path

from ashare_quant.execution_six.owner_worker import execute
from ashare_quant.execution_six.owner_worker import main as owner_main


def main():
    owner_main(
        lambda payload: execute(payload, pin_path=Path(__file__).with_name("query-owner-pin.json"))
    )
