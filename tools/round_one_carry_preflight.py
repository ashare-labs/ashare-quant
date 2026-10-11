"""Read the six unchanged carry cases' minimum inputs, without starting an engine.

Only frozen script CONFIG literals are read; reference account/order/trade answers
never become input. Run with the isolated AD dev5 environment and -I -B.
"""

import ast
import importlib.metadata
import importlib.util
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

CASE_IDS = ("B14", "B16", "B17", "B18", "B22", "B33")
INPUT_SHA = "c8fa02de8598e6dd82199c344034ddfd5f09a237c9648caeb568e757151d59e0"


def main():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "carry_receipt", root / "tools/round_one_carry_receipt.py"
    )
    receipt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receipt)
    guard = receipt.module("guard", root / "tools/round_one_tdx_receipt.py")
    sys.addaudithook(guard.readonly)
    inputs = root / "docs/round-one/input-bindings.json"
    if guard.sha(inputs) != INPUT_SHA:
        raise RuntimeError("CARRY_ORIGINAL_INPUTS_CHANGED")
    configs = []
    for item in json.loads(inputs.read_text())["cases"]:
        if item["case_id"] not in CASE_IDS:
            continue
        path = Path(item["folder"]) / "jq.py"
        if guard.sha(path) != item["files"]["jq.py"]:
            raise RuntimeError("CARRY_ORIGINAL_SCRIPT_CHANGED")
        values = [
            ast.literal_eval(node.value)
            for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in node.targets)
        ]
        if len(values) != 1:
            raise RuntimeError("CARRY_FROZEN_CONFIG_NOT_UNIQUE")
        configs.append(
            dict(case_id=item["case_id"], script_sha256=guard.sha(path), config=values[0])
        )

    wheel_path = (
        receipt.DELIVERY / "round-one-carry-delivery/dist/ashare_data-0.9.1.dev5-py3-none-any.whl"
    )
    if guard.sha(wheel_path) != receipt.WHEEL_SHA:
        raise RuntimeError("CARRY_OWNER_WHEEL_CHANGED")
    if importlib.metadata.version("ashare-data") != "0.9.1.dev5":
        raise RuntimeError("CARRY_OWNER_RUNTIME_CHANGED")
    site = Path(importlib.util.find_spec("ashare_data").origin).parent.parent
    checked = 0
    with zipfile.ZipFile(wheel_path) as archive:
        for name in archive.namelist():
            if name.startswith("ashare_data/") and not name.endswith("/"):
                if (site / name).read_bytes() != archive.read(name):
                    raise RuntimeError("CARRY_OWNER_INSTALLED_FILES_CHANGED")
                checked += 1
    from ashare_data import DataError, Store

    binding_path = receipt.DELIVERY / "round-one-data-delivery/priority-1/binding.json"
    if (
        guard.sha(binding_path)
        != "0aec2aa21d4a01a3d5180692bf56a7e59e5d07c7cf66a1e510f2f003da37f8a3"
    ):
        raise RuntimeError("CARRY_MARKET_BINDING_CHANGED")
    binding = json.loads(binding_path.read_text())
    if binding["dataset_id"] != "84d30dbf063b6538f2b71c7c314e92c288b481ec05160f804b44afa8ac14a3eb":
        raise RuntimeError("CARRY_MARKET_DATASET_CHANGED")
    view = Store(binding["store_path"]).jq_observations(binding["dataset_id"])
    reads = []

    def read(request, expected):
        try:
            value = view.read(**request).to_dict()
        except DataError as exc:
            if (
                expected != "outside_scope"
                or exc.message != "read clock outside source-bound security/session"
            ):
                raise
            reads.append(dict(request=request, error=dict(code=exc.code, message=exc.message)))
            return
        if value["status"] != expected:
            raise RuntimeError("CARRY_REQUIRED_READ_STATUS:" + str(request) + str(value))
        if expected == "available" and not value["rows"]:
            raise RuntimeError("CARRY_REQUIRED_ROWS_MISSING")
        for row in value["rows"]:
            if datetime.fromisoformat(row["available_at"]) > datetime.fromisoformat(
                request["as_of"]
            ):
                raise RuntimeError("CARRY_OWNER_FUTURE_ROW")
            if request["role"] == "strategy" and row["strategy_visible"] is not True:
                raise RuntimeError("CARRY_STRATEGY_ROW_NOT_VISIBLE")
            if request["role"] == "initialization" and (
                row["strategy_visible"] is not False
                or row["matching_eligible"] is not False
                or set(row["values"]) != {"close"}
            ):
                raise RuntimeError("CARRY_INITIALIZATION_ROLE_LEAK")
            if request["kind"] == "current" or request["role"] == "matching":
                if row["source_label_at"] != request["as_of"]:
                    raise RuntimeError("CARRY_CURRENT_OR_MATCHING_LABEL_MISMATCH")
        reads.append(dict(request=request, result=value))

    for security in ("000001.XSHE", "600000.XSHG"):
        for day in ("2024-03-04", "2024-03-05"):
            for clock in ("10:00", "10:01"):
                at = f"{day}T{clock}:00+08:00"
                for role in ("strategy", "matching"):
                    read(dict(security=security, kind="current", as_of=at, role=role), "available")
                read(
                    dict(
                        security=security,
                        kind="minute",
                        as_of=at,
                        role="matching",
                        start=at,
                        end=at,
                    ),
                    "available",
                )
                read(
                    dict(security=security, kind="minute", as_of=at, role="strategy", end=at),
                    "available",
                )
            read(
                dict(
                    security=security,
                    kind="daily",
                    as_of=f"{day}T15:30:00+08:00",
                    role="final_valuation",
                ),
                "available",
            )
        read(
            dict(
                security=security,
                kind="daily",
                as_of="2024-03-05T09:00:00+08:00",
                role="initialization",
                start="2024-03-04T00:00:00+08:00",
                end="2024-03-04T00:00:00+08:00",
            ),
            "available",
        )
        read(
            dict(
                security=security,
                kind="current",
                as_of="2024-03-05T09:00:00+08:00",
                role="strategy",
            ),
            "unavailable",
        )
    read(
        dict(
            security="000001.XSHE",
            kind="current",
            as_of="2024-03-06T10:00:00+08:00",
            role="strategy",
        ),
        "outside_scope",
    )
    print(
        json.dumps(
            dict(
                schema="aq.six-carry-input-preflight.v1",
                status="PUBLIC_INPUT_READS_VERIFIED_NOT_ENGINE_EXECUTION",
                frozen_inputs_sha256=INPUT_SHA,
                original_case_configs=configs,
                owner_version="0.9.1.dev5",
                owner_commit=receipt.OWNER_COMMIT,
                wheel=dict(path=str(wheel_path), sha256=receipt.WHEEL_SHA),
                installed_owner_files_checked=checked,
                market_binding=str(binding_path),
                market_binding_sha256=guard.sha(binding_path),
                dataset_id=binding["dataset_id"],
                dataset_publisher=binding["owner"],
                reads=reads,
                engine_run=False,
                new_jq_runs=0,
                network_used=False,
                historical_pit=False,
                strategy_future_access=False,
                interpretation="Inputs suffice for a declared short-window research hypothesis. Unknown lifecycle/action completeness remains a limitation, not a certified empty fact. B22 March6 may omit quotes only after native full liquidation is verified.",
            ),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
