"""Check frozen product evidence without reopening mutable sources or external packages."""

import hashlib
import json
import re


def owner_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def require(condition, field):
    if not condition:
        raise ValueError("PRODUCT_REFERENCE_CONFLICT:" + field)


def source_bindings(references):
    if references is None:
        return {}
    kinds = {"research": "daily_research", "execution": "daily_execution",
             "market": "historical_market", "corporate": "corporate_history"}
    return {role: {"kind": kinds[role], "binding_sha256": value["binding_sha256"]}
            for role, value in references.items() if role in kinds}


def validate(document):
    """Ordinary product exports require structured facts; legacy research stays explicit."""
    try:
        _validate(document)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("PRODUCT_REFERENCE_CONFLICT:malformed_evidence") from exc


def _validate(document):
    evidence = document.get("reference_evidence")
    provenance = document.get("product_release")
    ordinary = (evidence is not None or (provenance or {}).get("mode") == "covered_research"
                or (document.get("producer", {}).get("owner_api")
                    == "ashare_data.product.LocalDailyProduct"
                    and document.get("research_model") != "price_only_no_actions"))
    if not ordinary:
        return
    require(isinstance(evidence, dict), "reference_evidence")
    require(evidence["schema"] == "ad-product-reference-evidence.v1", "schema")
    ref, bindings, scope = (evidence[k] for k in ("reference_set", "source_bindings", "scope"))
    require(isinstance(ref["name"], str) and bool(ref["name"]), "reference_set.name")
    roles = ({"research", "execution", "corporate"} if ref["kind"] == "daily_execution"
             else {"market", "corporate"} if ref["kind"] == "historical_market" else set())
    require(roles and set(bindings) == roles, "source_bindings")
    kinds = {"research": "daily_research", "execution": "daily_execution",
             "market": "historical_market", "corporate": "corporate_history"}
    for role, binding in bindings.items():
        require(binding["kind"] == kinds[role] and isinstance(binding["binding_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", binding["binding_sha256"]), "binding." + role)
    require(set(document["securities"]) <= set(scope["securities"]), "scope.securities")
    days = set(document["owner_history_sha256"])
    require(days and scope["start"] <= min(days) <= max(days) <= scope["end"], "scope.dates")
    require(document.get("price_limit_policy") != "disabled_for_price_only_diagnostic"
            and document.get("research_model") != "price_only_no_actions", "mode")
    require(document.get("state_mode") == "retrospective_model", "state_mode")
    market = evidence["market"]
    require(market["kind"] == ref["kind"], "market.kind")
    rows = {(r["security"], r["date"]): r for r in market["rows"]}
    require(len(rows) == len(market["rows"]), "duplicate_reference_row")
    requests = {(s, d, side) for s, d in rows for side in ("BUY", "SELL")}
    if ref["kind"] == "daily_execution":
        admission = market["admission"]
        require(market["report"]["binding_sha256"] == bindings["execution"]["binding_sha256"]
                and market["report"]["research_binding_sha256"] == bindings["research"]["binding_sha256"],
                "market.report_bindings")
        require(market["report"]["historical_pit"] is False
                and market["report"]["execution_permission"] is False, "market.certification")
        require(admission["ready"] is True and admission["purpose"] == "retrospective_simulation"
                and not any(issue["blocking"] for issue in admission["issues"]), "market.admission")
        require({(r["security"], r["date"], r["side"]) for r in admission["requests"]} == requests,
                "market.requests")
        require(set(admission["required_assumptions"]) <= set(market["accepted_assumptions"])
                == set(admission["accepted_assumptions"]), "market.assumptions")
        require(market["cash_credit_policy"] == admission["cash_credit_policy"]
                == "pay_date_before_open", "market.cash_policy")
    else:
        admissions = market["admissions"]
        require(market["descriptor"]["binding_sha256"] == bindings["market"]["binding_sha256"]
                and market["descriptor"]["historical_pit"] is False
                and market["descriptor"]["execution_permission"] is False, "market.descriptor")
        require({(r["security"], r["date"], r["side"]) for r in admissions} == requests,
                "market.requests")
        require(market["accepted_assumptions"]
                == {"accept_daily_replay": True, "accept_limit_model": True}, "market.assumptions")
        for entry in admissions:
            require(entry["state_sha256"] == owner_digest(rows[entry["security"], entry["date"]]),
                    "market.state_binding")
            require(not entry["issues"] or (entry["issues"] == ["DAILY_SUSPENDED"]
                    and rows[entry["security"], entry["date"]]["suspended"]["value"] is True),
                    "market.admission")
    for row in document["daily"]:
        if row["date"] not in days:
            continue
        key = row["security"], row["date"]
        require(key in rows, "missing_reference_row:" + ":".join(key))
        state = rows[key]
        require(row["state_evidence"]["reference_sha256"] == owner_digest(state), "row_hash")
        require(not state.get("dependency_conflicts"), "dependency_conflicts")
        for field in ("suspended", "reference_price", "upper_limit", "lower_limit"):
            fact = state[field]
            require(fact["value"] is not None and fact["source_refs"]
                    and fact["status"] in {"source_observed", "document_extracted", "derived"},
                    field + ".source")
            if field != "reference_price":
                require(row[field] == fact["value"], field + ".value")
        if any(state[f]["status"] == "derived" for f in ("upper_limit", "lower_limit")):
            require(type(state["is_st"]["value"]) is bool and state["is_st"]["source_refs"]
                    and state["is_st"]["status"] in {"source_observed", "document_extracted", "derived"},
                    "is_st.dependency")
    schedule = document["corporate_schedule"]
    require(schedule["binding_sha256"] == bindings["corporate"]["binding_sha256"]
            == evidence["corporate"]["binding_sha256"], "corporate_binding")
    require(owner_digest(schedule) == evidence["corporate"]["response_sha256"], "corporate_response")
    if provenance is not None:
        manifest, coverage = provenance["manifest"], document["product_coverage"]
        require(owner_digest(manifest) == provenance["dataset_id"], "manifest")
        require(provenance["mode"] == manifest["mode"] == "covered_research", "release_mode")
        require(manifest["source_bindings"] == bindings == coverage["source_bindings"], "release_bindings")
        require(manifest["reference_set"] == ref == coverage["reference_set"], "release_reference_set")
        require(manifest["securities"] == scope["securities"]
                and manifest["start"] == scope["start"] and manifest["end"] == scope["end"]
                and manifest["source_selection"] == coverage["scope"]["source"], "manifest_scope")
        require(scope == {k: coverage["scope"][k] for k in ("securities", "start", "end")},
                "coverage_scope")
        require(coverage["ready"] is True and not coverage["gaps"], "coverage_gaps")
        require(coverage["reference_evidence_sha256"] == owner_digest(evidence), "coverage_evidence")
        require(owner_digest({k: v for k, v in coverage.items() if k != "coverage_sha256"})
                == coverage["coverage_sha256"] == manifest["coverage_sha256"], "coverage_sha256")
        require(provenance["portfolio_input_sha256"] == manifest["portfolio_input_sha256"]
                and provenance["corporate_schedule_sha256"] == manifest["corporate_schedule_sha256"],
                "release_files")
        from ashare_quant.portfolio.inputs import digest as input_digest

        require(provenance["corporate_schedule_semantic_sha256"] == input_digest(schedule)
                and provenance["portfolio_input_semantic_sha256"]
                == document["corporate_source_replacement"]["original_document_sha256"],
                "release_semantic_identity")
