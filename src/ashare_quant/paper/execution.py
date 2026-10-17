"""Immutable per-day AD deliveries. Control receipts only; no balances or orders."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from ashare_quant.contracts import DomainError
from ashare_quant.paper.contracts import RiskPolicy, SessionConfig
from ashare_quant.paper.risk import require
from ashare_quant.storage.artifacts import atomic, canonical, digest, lock, regular, sha


def copy_package(binding, expected, target):
    path = Path(binding)
    raw = regular(path)
    require(sha(raw) == expected, "PAPER_AD_BINDING_CHANGED")
    target.mkdir(parents=True, exist_ok=True)
    for item in json.loads(raw)["files"]:
        rel = Path(item["path"])
        require(not rel.is_absolute() and ".." not in rel.parts, "PAPER_AD_SOURCE_PATH")
        data = regular(path.parent / rel)
        require(
            len(data) == item["bytes"] and sha(data) == item["sha256"],
            "PAPER_AD_SOURCE_CHANGED",
            str(rel),
        )
        destination = target / rel
        if destination.exists():
            require(regular(destination) == data, "PAPER_AD_SOURCE_CHANGED")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic(destination, data)
    atomic(target / "binding.json", raw)


def reader(root, spec, operation, day=None, orders=None):
    from ashare_quant.providers.runtime import verify
    current = verify()
    require(spec["runtime_inventory"] == current["files"], "PAPER_AD_RUNTIME_CHANGED")
    require(sha(regular(root / "owner.whl")) == spec["wheel_sha256"] == current["wheel_sha256"],
            "PAPER_AD_RUNTIME_CHANGED")
    paths = [str(Path(__file__).resolve().parents[2]),
             str(metadata.distribution("rqalpha").locate_file(""))]
    boot = "import json,sys;sys.path[:0]=json.loads(sys.argv[1]);from ashare_quant.paper.execution_owner import main;main()"
    try:
        run = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", boot, json.dumps(paths)],
            input=canonical(
                {
                    "root": str(root),
                    "spec": spec,
                    "operation": operation,
                    "day": day,
                    "orders": orders,
                }
            ),
            capture_output=True,
            timeout=60,
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1"},
        )
        require(run.returncode == 0, "PAPER_AD_READER_FAILED", run.stderr.decode()[-2000:])
        result = json.loads(run.stdout)
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise DomainError("PAPER_AD_READER_FAILED", str(exc), 409) from exc
    require(
        "error" not in result,
        "PAPER_AD_READER_REJECTED",
        json.dumps(result.get("error"), ensure_ascii=False),
    )
    return result["result"]


def contract_metadata(path, expected, research_binding, execution_binding, ad_commit):
    """Bind the reviewed consumer contract; resolve references against sealed data."""
    raw = regular(Path(path))
    require(sha(raw) == expected, "PAPER_AD_CONTRACT_CHANGED")
    contract = json.loads(raw)
    require(
        contract["schema"] == "ad.daily-execution-consumer-contract.v2"
        and contract["source_commit"] == ad_commit
        and contract["path_base"] == "package_root",
        "PAPER_AD_CONTRACT_CHANGED",
    )
    require(
        contract["research_binding_sha256"] == sha(regular(Path(research_binding)))
        and contract["binding_sha256"] == sha(regular(Path(execution_binding))),
        "PAPER_AD_CONTRACT_CHANGED",
    )
    refs = {}
    for field in ("existing_cash_event_contract", "order_impact_summary"):
        rel = Path(contract[field])
        require(not rel.is_absolute() and ".." not in rel.parts, "PAPER_AD_SOURCE_PATH")
        refs[field] = json.loads(regular(Path(path).parent / rel))
    rel = Path(contract["unresolved"]["details"])
    require(not rel.is_absolute() and ".." not in rel.parts, "PAPER_AD_SOURCE_PATH")
    gaps = json.loads(regular(Path(path).parent / rel))
    cash = refs["existing_cash_event_contract"]
    for name, binding in (
        ("research_event", research_binding),
        ("execution_event", execution_binding),
    ):
        source, pointer = cash["paths"][name].split("#")
        allowed = "binding-data.json" if name == "research_event" else "execution-data.json"
        require(Path(source).name == allowed, "PAPER_AD_CONTRACT_CHANGED")
        event = json.loads(regular(Path(binding).parent / allowed))
        try:
            for part in pointer.strip("/").split("/"):
                event = event[int(part)] if isinstance(event, list) else event[part]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DomainError(
                "PAPER_AD_CONTRACT_CHANGED", "Invalid cash event pointer", 409
            ) from exc
        keys = (
            "security",
            "type",
            "announcement_date",
            "record_date",
            "ex_date",
            "payment_date",
            "cash_per_share",
            "ex_price_cash_deduction",
            "bonus_share_ratio",
            "capitalization_share_ratio",
        )
        require(
            all(event.get(k) == cash["source_event"][k] for k in keys),
            "PAPER_AD_CONTRACT_CHANGED",
            "Cash event pointer does not resolve to the reviewed event",
        )
    actual = json.loads(regular(Path(execution_binding).parent / "execution-data.json"))
    cells = {
        (r["security"], r["date"], f)
        for r in actual["states"]
        for f in ("is_st", "upper_limit", "lower_limit")
        if r[f]["value"] is None
    }
    require(
        cells == {(r["security"], r["date"], r["field"]) for r in gaps["exact_issues"]}
        and len(cells) == contract["unresolved"]["field_count"],
        "PAPER_AD_CONTRACT_CHANGED",
    )
    return {"sha256": expected, "contract": contract, "references": refs, "exact_gap_scope": gaps}


def bind(
    service,
    manifest_path,
    research_binding,
    research_sha,
    execution_binding,
    execution_sha,
    wheel,
    wheel_sha,
    ad_commit,
    strategy,
    window,
    account,
    *,
    accepted_assumptions,
    accept_listing_continuity,
    cash_credit_policy,
    risk=None,
    consumer_contract=None,
    consumer_contract_sha=None,
):
    from ashare_quant.paper.replay import strategy_version

    require(
        cash_credit_policy == "pay_date_before_open",
        "PAPER_ACTION_POLICY_MISMATCH",
        "The native paper policy currently supports explicit 09:00 research cash credit only.",
    )
    require(accept_listing_continuity, "PAPER_LISTING_MODEL_REQUIRED")
    manifest_raw = regular(Path(manifest_path))
    manifest = json.loads(manifest_raw)
    scope = next((w for w in manifest["windows"] if w["id"] == window), None)
    require(scope is not None, "PAPER_WINDOW_UNKNOWN")
    wheel_raw = regular(Path(wheel))
    require(sha(wheel_raw) == wheel_sha, "PAPER_AD_RUNTIME_CHANGED")
    spec = {
        "research_sha256": research_sha,
        "execution_sha256": execution_sha,
        "wheel_sha256": wheel_sha,
        "ad_code_commit": ad_commit,
        "window": window,
        "accepted_assumptions": sorted(accepted_assumptions),
        "accept_listing_continuity": accept_listing_continuity,
        "cash_credit_policy": cash_credit_policy,
    }
    require(bool(consumer_contract) == bool(consumer_contract_sha), "PAPER_AD_CONTRACT_CHANGED")
    if consumer_contract:
        spec["consumer_contract"] = contract_metadata(
            consumer_contract, consumer_contract_sha, research_binding, execution_binding, ad_commit
        )
    source_id = digest(spec)
    source = service.root / "sources" / source_id
    source.mkdir(parents=True, exist_ok=True)
    with lock(source / "import.lock"):
        copy_package(research_binding, research_sha, source / "research")
        copy_package(execution_binding, execution_sha, source / "execution")
        if consumer_contract:
            directory = source / "consumer"
            directory.mkdir(exist_ok=True)
            atomic(directory / "consumer-contract.json", regular(Path(consumer_contract)))
            meta = spec["consumer_contract"]
            for field, value in meta["references"].items():
                dest = directory / meta["contract"][field]
                dest.parent.mkdir(parents=True, exist_ok=True)
                atomic(dest, value)
            dest = directory / meta["contract"]["unresolved"]["details"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic(dest, meta["exact_gap_scope"])
        atomic(source / "owner.whl", wheel_raw)
        from ashare_quant.providers.runtime import verify
        paired = verify()
        require(wheel_sha == paired["wheel_sha256"] and ad_commit == paired["commit"],
                "PAPER_AD_RUNTIME_CHANGED", "Use the same installed data-owner candidate")
        spec["runtime_inventory"] = paired["files"]
        description = reader(source, spec, "describe")
        require(set(description["securities"]) == set(manifest["securities"]), "PAPER_TARGET_SET")
        registry = {"spec": spec, "description": description}
        if (source / "source.json").exists():
            require(
                json.loads(regular(source / "source.json")) == registry, "PAPER_AD_SOURCE_CHANGED"
            )
        else:
            atomic(source / "source.json", registry)
    binding = {
        "schema": "paper-execution-binding.v1",
        "source_id": source_id,
        "source_sha256": digest(registry),
        "input_sha256": source_id,
        "manifest_sha256": sha(manifest_raw),
        "strategy_id": strategy,
        "window": scope,
        "real_market_data": True,
        "accept_owner_state_model": True,
        "cash_dividend_policy": "gross_zero_withholding_0900",
    }
    execution = manifest["execution"]
    fees = execution["fees"]
    require(
        execution["gross_target_weight"] == "0.98"
        and execution["lot_size"] == 100
        and fees["buy_commission_rate"] == fees["sell_commission_rate"]
        and fees["slippage"] == fees["transfer_fee"] == "0",
        "PAPER_FROZEN_STRATEGY_PARAMETERS",
    )
    config = SessionConfig(
        account_id=account,
        strategy_id=strategy,
        strategy_version=strategy_version(manifest, strategy),
        scenario_manifest_sha256=sha(manifest_raw),
        replay_binding_sha256=digest(binding),
        initial_cash=execution["initial_cash"],
        universe=tuple(manifest["securities"]),
        data_mode="historical_research",
        cash_dividend_policy="gross_zero_withholding_0900",
        execution_admission_scope="per_order" if consumer_contract else "whole_window",
        commission_rate=fees["buy_commission_rate"],
        minimum_commission=fees["minimum_commission"],
        sell_tax_rate=fees["sell_tax_rate"],
        risk=RiskPolicy.model_validate(risk or {}),
    )
    session = service.create(config)
    replay = service.root / session["id"] / "replay"
    replay.mkdir(exist_ok=True)
    with lock(service.root / session["id"] / "import.lock"):
        for name, raw in (("manifest.json", manifest_raw), ("binding.json", canonical(binding))):
            path = replay / name
            if path.exists():
                require(regular(path) == raw, "PAPER_REPLAY_INPUT_CHANGED")
            else:
                atomic(path, raw)
    return {"session_id": session["id"], "binding": binding, "admission": description["admission"]}


class ExecutionInputs:
    def __init__(self, service, sid, binding):
        self.service, self.sid, self.binding = service, sid, binding
        self.source = service.root / "sources" / binding["source_id"]
        self.registry = json.loads(regular(self.source / "source.json"))
        require(digest(self.registry) == binding["source_sha256"], "PAPER_AD_SOURCE_CHANGED")
        d = self.registry["description"]
        spec = self.registry["spec"]
        self.ids = tuple(d["securities"])
        self.calendar = d["window"]["sessions"]
        scope = binding["window"]
        self.days = [x for x in self.calendar if scope["start"] <= x <= scope["end"]]
        self.previous = {x: self.calendar[self.calendar.index(x) - 1] for x in self.days}
        self.observation_admission = d.get("observation_admission", d["admission"])
        self.gaps = [i for i in self.observation_admission["issues"] if i["blocking"]]
        self.sha256 = binding["source_id"]
        self.actions = []
        self.packet = None
        self.document = {
            "producer": d["descriptor"]["producer"],
            "real_market_data": True,
            "state_mode": "ad_execution_retrospective_model",
            "limitations": d["limitations"]
            + [
                "Actual dividend intraday availability and historical source delivery remain unknown.",
                "Per-day delivery from a sealed historical source is not an online market-data feed.",
            ],
            "assumptions": [
                {"id": k, "text": d["descriptor"]["assumptions"][k]}
                for k in spec["accepted_assumptions"]
            ]
            + [
                {
                    "id": "scoped_listing_continuity",
                    "text": "Use source listing dates and bounded continuity for these fixed samples.",
                },
                {
                    "id": "gross_zero_withholding_0900",
                    "text": "Explicit investor research policy: gross cash, zero withholding, payment-date 09:00; source payment time remains null.",
                },
            ],
            "source_refs": [
                {
                    "research_binding_sha256": spec["research_sha256"],
                    "execution_binding_sha256": spec["execution_sha256"],
                    "ad_code_commit": spec["ad_code_commit"],
                    "wheel_sha256": spec["wheel_sha256"],
                }
            ],
            "owner_descriptor": {"binding_sha256": spec["execution_sha256"]},
        }
        self.directory = service.root / sid / "deliveries"

    def preflight(self):
        return {**self.observation_admission, "gaps": self.observation_admission["issues"]}

    def order_permissions(self, day, targets, quantities):
        if not self.registry["spec"].get("consumer_contract"):
            return []
        requests = [
            {"date": day, "security": s, "side": "BUY" if targets[s] > quantities[s] else "SELL"}
            for s in self.ids
            if targets[s] != quantities[s]
        ]
        result = reader(self.source, self.registry["spec"], "orders", day, requests)
        if "blocked" in result:
            blocked = result["blocked"]
            detail = {
                **blocked,
                "issues": [i for i in blocked["issues"] if i["blocking"]],
                "observation_warning_count": sum(not i["blocking"] for i in blocked["issues"]),
            }
            raise DomainError(
                "PAPER_AD_ORDER_NOT_ADMITTED", json.dumps(detail, ensure_ascii=False), 409
            )
        return result["permissions"]

    def deliveries(self):
        result, parent = [], None
        with self.service.db() as db:
            entries = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM paper_deliveries WHERE session_id=? ORDER BY day", (self.sid,)
                )
            ]
        for index, entry in enumerate(entries):
            path = self.directory / (entry["day"] + ".json")
            raw = regular(path)
            require(sha(raw) == entry["packet_sha256"], "PAPER_DELIVERY_CHANGED")
            packet = json.loads(raw)
            require(
                index < len(self.days)
                and path.stem == self.days[index] == packet["day"]
                and packet["source_id"] == self.binding["source_id"]
                and packet["previous_packet_sha256"] == parent,
                "PAPER_DELIVERY_CHAIN_CHANGED",
            )
            parent = digest(packet)
            result.append(
                {
                    "day": packet["day"],
                    "packet_sha256": parent,
                    "received_at": packet["received_at"],
                }
            )
        return result

    def select(self, day):
        values = self.deliveries()
        require(
            any(x["day"] == day for x in values),
            "PAPER_EXECUTION_DAY_MISSING",
            f"{day}: append the AD day packet before advancing; no missing-day defaults.",
        )
        packet = json.loads(regular(self.directory / (day + ".json")))
        self.packet = packet["data"]
        self.sha256 = digest(packet)
        self.actions = self.packet["actions"]
        require(
            self.packet["admission"]["ready"]
            and (
                self.packet["report"].get("observation_admitted")
                if self.registry["spec"].get("consumer_contract")
                else self.packet["report"]["simulation_admitted"]
            ),
            "PAPER_AD_NOT_ADMITTED",
        )

    def row(self, security, day):
        require(
            self.packet is not None and self.packet["day"] == day, "PAPER_EXECUTION_DAY_MISSING"
        )
        return self.packet["rows"][security]

    def history(self, day):
        require(
            self.packet is not None and self.packet["day"] == day, "PAPER_EXECUTION_DAY_MISSING"
        )
        return {s: tuple(values) for s, values in self.packet["history"].items()}


def fork(service, sid, account, risk):
    from ashare_quant.paper.replay import load

    _, config = service.session(sid)
    binding, inputs = load(service, sid, config)
    spec = inputs.registry["spec"]
    source = inputs.source
    contract = spec.get("consumer_contract")
    return bind(
        service,
        service.root / sid / "replay/manifest.json",
        source / "research/binding.json",
        spec["research_sha256"],
        source / "execution/binding.json",
        spec["execution_sha256"],
        source / "owner.whl",
        spec["wheel_sha256"],
        spec["ad_code_commit"],
        config.strategy_id,
        binding["window"]["id"],
        account,
        accepted_assumptions=spec["accepted_assumptions"],
        accept_listing_continuity=spec["accept_listing_continuity"],
        cash_credit_policy=spec["cash_credit_policy"],
        risk=risk,
        consumer_contract=source / "consumer/consumer-contract.json" if contract else None,
        consumer_contract_sha=contract["sha256"] if contract else None,
    )


def ingest(service, sid, day, expected_parent, *, fault=None):
    from ashare_quant.paper.replay import load

    _, config = service.session(sid)
    binding, inputs = load(service, sid, config)
    require(isinstance(inputs, ExecutionInputs), "PAPER_EXECUTION_BINDING_REQUIRED")
    require(day in inputs.days, "PAPER_REPLAY_DATE_OUT_OF_SCOPE")
    with lock(service.root / sid / "delivery.lock"):
        path = inputs.directory / (day + ".json")
        received = inputs.deliveries()
        if any(x["day"] == day for x in received):
            packet = json.loads(regular(path))
            require(
                packet["expected_account_parent"] == expected_parent,
                "PAPER_DELIVERY_REQUEST_CONFLICT",
            )
            return {
                "day": day,
                "packet_sha256": digest(packet),
                "received_at": packet["received_at"],
                "already_received": True,
            }
        session, _ = service.session(sid)
        require(session["head"] == expected_parent, "PAPER_PARENT_CONFLICT")
        require(
            len(received) < len(inputs.days) and inputs.days[len(received)] == day,
            "PAPER_DELIVERY_SEQUENCE",
        )
        require(
            (not received and session["last_day"] is None)
            or (received and session["last_day"] == received[-1]["day"]),
            "PAPER_PRIOR_DAY_NOT_COMMITTED",
        )
        data = reader(inputs.source, inputs.registry["spec"], "day", day)
        require(
            "blocked" not in data,
            "PAPER_AD_NOT_ADMITTED",
            json.dumps(data.get("blocked"), ensure_ascii=False),
        )
        require(
            data["day"] == day
            and data["admission"]["ready"]
            and (
                data["report"].get("observation_admitted")
                if inputs.registry["spec"].get("consumer_contract")
                else data["report"]["simulation_admitted"]
            ),
            "PAPER_AD_NOT_ADMITTED",
        )
        packet = {
            "schema": "paper-ad-day-delivery.v1",
            "source_id": binding["source_id"],
            "day": day,
            "previous_packet_sha256": received[-1]["packet_sha256"] if received else None,
            "expected_account_parent": expected_parent,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "delivery_kind": "sealed_historical_source_increment",
            "data": data,
        }
        inputs.directory.mkdir(exist_ok=True)
        if path.exists():
            staged = json.loads(regular(path))
            require(
                {k: v for k, v in staged.items() if k != "received_at"}
                == {k: v for k, v in packet.items() if k != "received_at"},
                "PAPER_DELIVERY_CHANGED",
            )
            packet = staged
        else:
            atomic(path, packet)
        if fault == "after_packet_write":
            raise DomainError(
                "PAPER_DELIVERY_RESPONSE_LOST",
                "Packet staged; retry the exact request to revalidate and admit it.",
                409,
            )
        with service.db() as db:
            db.execute(
                "INSERT INTO paper_deliveries VALUES(?,?,?,?)",
                (sid, day, digest(packet), packet["received_at"]),
            )
            service.event(
                db,
                sid,
                day,
                "ad_day_received",
                {
                    "packet_sha256": digest(packet),
                    "previous_packet_sha256": packet["previous_packet_sha256"],
                    "received_at": packet["received_at"],
                    "delivery_kind": packet["delivery_kind"],
                },
            )
        if fault == "after_packet_commit":
            raise DomainError(
                "PAPER_DELIVERY_RESPONSE_LOST",
                "Packet admitted; retry the exact request to confirm the receipt.",
                409,
            )
        return {
            "day": day,
            "packet_sha256": digest(packet),
            "received_at": packet["received_at"],
            "already_received": False,
        }
