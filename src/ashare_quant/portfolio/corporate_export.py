"""Attach AD CorporateHistoryView's pinned public response to an owner price export.

Run in the separate AD environment. No retrieval, database writes or account
execution takes place here. Existing prices, market state and history receipts
are preserved; only the explicitly superseded corporate source is replaced.
"""

import argparse
import copy
import hashlib
import inspect
import json
from pathlib import Path

from ashare_quant.portfolio.corporate import MODEL, schedule_rows
from ashare_quant.portfolio.inputs import digest, require


def attach(document, schedule, *, accept_date_model=False, owner_module_sha256=None):
    require(accept_date_model is True, "CORPORATE_DATE_MODEL_NOT_ACCEPTED")
    require("corporate_schedule" not in document, "CORPORATE_SCHEDULE_ALREADY_ATTACHED")
    require(
        document.get("research_model") != "price_only_no_actions", "CORPORATE_PRICE_ONLY_CONFLICT"
    )
    value = copy.deepcopy(document)
    value["corporate_source_replacement"] = {
        "original_document_sha256": digest(document),
        "actions": value.pop("actions", []),
        "action_coverage": value.get("action_coverage"),
        "additional_gaps": value.get("additional_gaps", []),
    }
    value["actions"] = []
    value["corporate_schedule"] = copy.deepcopy(schedule)
    value["corporate_account_model"] = dict(MODEL)
    schedule_rows(value, value["securities"], schedule["coverage"], value["calendar"])
    value["action_coverage"] = {
        "status": "source_covered",
        "scope": "AD CorporateHistoryView source-scoped distribution schedule; no market-wide absence proof",
        "binding_sha256": schedule["binding_sha256"],
        "response_sha256": digest(schedule),
        "coverage": copy.deepcopy(schedule["coverage"]),
    }
    value["additional_gaps"] = [
        g
        for g in value.get("additional_gaps", [])
        if g.get("field")
        not in {
            "corporate_actions",
            "cash_payment_date",
            "share_credit_or_tradable_date",
        }
    ]
    value.setdefault("owner_api_reads", []).append(
        {
            "operation": "CorporateHistoryView.export_aq",
            "response_sha256": digest(schedule),
            "binding_sha256": schedule["binding_sha256"],
            "owner_module_sha256": owner_module_sha256,
            "tax_model": "gross_cash_no_tax",
            "start": schedule["coverage"]["start"],
            "end": schedule["coverage"]["end"],
        }
    )
    value.setdefault("source_refs", []).append(
        {
            "owner_api": "ashare_data.CorporateHistoryView.export_aq",
            "binding_sha256": schedule["binding_sha256"],
            "response_sha256": digest(schedule),
        }
    )
    value.setdefault("assumptions", []).append({"corporate_account_model": dict(MODEL)})
    value.setdefault("limitations", []).extend(
        [
            "Corporate dates are applied before open under an explicit research model; actual intraday credit is unverified.",
            "Gross dividends without withholding; nonintegral share entitlement and post-settlement source corrections block execution.",
            "Corporate coverage is source scoped, not historical PIT, all-market absence proof, or certified historical trading limits.",
        ]
    )
    value["economic_acceptance"] = "conditional_research_only"
    return value


def export(document, binding, binding_sha256, start, end, *, accept_date_model=False):
    from ashare_data import CorporateHistoryView

    owner = CorporateHistoryView(binding, expected_binding_sha256=binding_sha256)
    schedule = owner.export_aq(
        document["securities"],
        start=start,
        end=end,
        tax_model="gross_cash_no_tax",
        accept_source_scoped_coverage=True,
    )
    return attach(
        document,
        schedule,
        accept_date_model=accept_date_model,
        owner_module_sha256=hashlib.sha256(
            Path(inspect.getfile(CorporateHistoryView)).read_bytes()
        ).hexdigest(),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--accept-date-model", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from ashare_quant.execution.rqalpha.guards import install_network_guard

    install_network_guard()
    value = export(
        json.loads(args.input.read_bytes()),
        args.binding,
        args.binding_sha256,
        args.start,
        args.end,
        accept_date_model=args.accept_date_model,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "schedule_sha256": digest(value["corporate_schedule"]),
                "model": MODEL,
            }
        )
    )


if __name__ == "__main__":
    main()
