"""Read the new B26 point through AD dev4's public API; never admit execution."""

import importlib.metadata
import importlib.util
import json
import sys
import zipfile
from pathlib import Path


def main():
    spec = importlib.util.spec_from_file_location(
        "receipt_guard", Path(__file__).with_name("round_one_tdx_receipt.py")
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    sys.addaudithook(guard.readonly)
    runtime_path, product_path = map(Path, sys.argv[1:])
    runtime, product = [json.loads(p.read_text()) for p in (runtime_path, product_path)]
    wheel = runtime["wheel"]
    if guard.sha(wheel["path"]) != wheel["sha256"]:
        raise RuntimeError("POINT_RECEIPT_WHEEL_MISMATCH")
    if (
        importlib.metadata.version("ashare-data") != runtime["ad_version"]
        or runtime["ad_version"] != product["ad_version"]
    ):
        raise RuntimeError("POINT_RECEIPT_RUNTIME_MISMATCH")
    site = Path(importlib.util.find_spec("ashare_data").origin).parent.parent
    files = {}
    with zipfile.ZipFile(wheel["path"]) as archive:
        for name in archive.namelist():
            if name.startswith("ashare_data/") and not name.endswith("/"):
                if (site / name).read_bytes() != archive.read(name):
                    raise RuntimeError("POINT_RECEIPT_INSTALLED_MISMATCH:" + name)
                files[name] = guard.sha(site / name)
    from ashare_data import DataError, Store

    view = Store(product["owner_store"]).tdx_points(product["dataset_id"])
    reads = []
    for role in ("retrospective_point_research", "strategy", "matching"):
        request = dict(security="000001.XSHE", at="2024-06-14T10:00:00+08:00", role=role)
        try:
            result = view.read(**request)
        except DataError as exc:
            reads.append(dict(request=request, error=dict(code=exc.code, message=exc.message)))
        else:
            if role != "retrospective_point_research" or result != product["read"]:
                raise RuntimeError("POINT_RECEIPT_PUBLIC_RESULT_OR_ROLE_MISMATCH")
            reads.append(dict(request=request, result=result))
    print(
        json.dumps(
            dict(
                schema="aq.b26.point-receipt.v1",
                product_binding=str(product_path),
                product_binding_sha256=guard.sha(product_path),
                dataset_id=product["dataset_id"],
                runtime_binding=str(runtime_path),
                runtime_binding_sha256=guard.sha(runtime_path),
                wheel=wheel,
                installed_owner_files=files,
                reads=reads,
                engine_run=False,
                network_used=False,
                strategy_or_matching_admitted=False,
                historical_pit=False,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
