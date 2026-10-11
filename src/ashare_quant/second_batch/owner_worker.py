"""Fresh AD-only reader process. No strategy, order or account inputs."""

import json
import sys
from pathlib import Path

from ashare_data import PremarketReferenceView, Store
from ashare_data.independent_queries import ROLE, IndependentQueryView


def main():
    binding = json.loads(Path(sys.argv[1]).read_text())
    store = Store(binding["store"])
    view = store.recorded_market(binding["dataset_id"])
    legacy = store.jq_observations(binding["legacy_dataset_id"])
    pin = binding["independent"]
    independent = IndependentQueryView(pin["path"], expected_binding_sha256=pin["sha256"])
    daily = {}
    for product in ["b45-daily", "b40-44-daily"]:
        result = independent.read_daily(product, "000001.XSHE", source="sina-pingan", role=ROLE)
        daily[product] = result
    for line in sys.stdin:
        try:
            req = json.loads(line)
            op, args = req["operation"], req.get("parameters", {})
            if op == "describe":
                answer = view.describe()
            elif op in {"current", "query", "market", "engine_quote"}:
                answer = getattr(view, op)(**args).to_dict()
            elif op == "daily":
                result = daily[args["product"]]
                answer = {
                    **result,
                    "rows": [r for r in result["rows"] if r["date"] == args["date"]],
                }
            elif op == "legacy":
                answer = legacy.read(**args).to_dict()
            elif op == "actions":
                answer = independent.read_action_evidence("b45-action", "000001.XSHE", role=ROLE)
            elif op == "premarket_reference":
                pin = binding["premarket"]
                answer = PremarketReferenceView(
                    pin["path"], expected_binding_sha256=pin["sha256"]
                ).read(**args, role="premarket_reference_reconstruction")
            elif op == "valuation_point":
                pin = binding.get("valuation_points", {}).get(
                    args["security"] + "|" + args["at"][:10]
                )
                if pin is None:
                    answer = dict(status="unavailable", rows=[], reason="NO_BOUND_POINT_PRODUCT")
                else:
                    if pin["read_scope"] != "valuation_only":
                        raise ValueError("SECOND_POINT_SCOPE")
                    answer = store.tdx_points(pin["dataset_id"]).read(
                        **args, role="retrospective_point_research"
                    )
            else:
                raise ValueError("OWNER_UNKNOWN_OPERATION")
            print(json.dumps(dict(ok=True, value=answer), allow_nan=False), flush=True)
        except Exception as exc:
            print(
                json.dumps(
                    dict(
                        ok=False,
                        error=type(exc).__name__,
                        code=getattr(exc, "code", None),
                        message=str(exc),
                    )
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
