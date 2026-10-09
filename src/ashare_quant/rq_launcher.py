"""One trusted engine process, one OS-locked writer, no external data/network."""

import argparse
import fcntl
import os
from contextlib import ExitStack
from pathlib import Path


def run():
    from rqprobe.guards import install_network_guard
    install_network_guard()

    import rqalpha.main
    import rqalpha.utils.config as config_module
    from rqalpha.model.order import Order
    from rqalpha.model.trade import Trade

    from ashare_quant.identity import source_sha256
    from ashare_quant.rq_fixture import ScenarioProvider
    from ashare_quant.rq_mod import atomic_json
    from ashare_quant.rq_specs import rq_specifications
    from ashare_quant.runtime import runtime_fingerprint
    from rqprobe.checkpoint import AtomicFile, Counter, StrictHelper
    from rqprobe.runtime import checkpoint_identity

    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=list(rq_specifications()))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--account", default="standalone-evaluation")
    parser.add_argument("--account-lock")
    parser.add_argument("--invocation-id", default="standalone")
    parser.add_argument("--run-id", default="standalone")
    parser.add_argument("--manifest-sha256", default="standalone")
    parser.add_argument("--control-db")
    parser.add_argument("--lease-token")
    parser.add_argument("--inherited-lock", type=int, action="append", default=[])
    parser.add_argument("--reconcile-only", action="store_true")
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    account_path = Path(args.account_lock) if args.account_lock else root / "account.lock"
    account_path.parent.mkdir(parents=True, exist_ok=True)
    # Host and child share inherited lock descriptions; the host holds them through SQL finalization.
    # A standalone probe retains its original local writer locks.
    with ExitStack() as locks:
        if args.inherited_lock:
            if len(args.inherited_lock) != 2:
                raise RuntimeError("INVALID_INHERITED_LOCKS")
            for fd, path in zip(args.inherited_lock, (account_path, root / "writer.lock")):
                if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (path.stat().st_dev, path.stat().st_ino):
                    raise RuntimeError("INVALID_INHERITED_LOCK_PATH")
                locks.callback(os.close, fd)
        else:
            for path in (account_path, root / "writer.lock"):
                lock = locks.enter_context(path.open("a+b"))
                fcntl.flock(lock, fcntl.LOCK_EX)
        if args.control_db:
            from ashare_quant.admission import validate_manifest
            from ashare_quant.native_control import check_control, configure
            from ashare_quant.store import digest
            configure(args.control_db, args.run_id, args.lease_token)
            accepted = check_control(allow_cancel=args.reconcile_only)
            validate_manifest(accepted)
            if digest(accepted) != args.manifest_sha256 or accepted.request.account_id != args.account:
                raise RuntimeError("ACCEPTED_MANIFEST_MISMATCH")
            if accepted.strategy.definition.scenario != args.scenario:
                raise RuntimeError("ACCEPTED_SCENARIO_MISMATCH")
        spec = rq_specifications()[args.scenario]
        frequency = spec["parameters"]["frequency"]
        records = ScenarioProvider(args.scenario).records(frequency)
        events_path, checkpoint = root / "inputs.json", root / "native.chk"
        publication, result = root / "publication.json", root / "receipt.json"
        if not args.reconcile_only:
            atomic_json(events_path, records)
        source = f"SCENARIO = {args.scenario!r}\n" + Path(__file__).with_name("rq_strategy.py").read_text()
        requested = {
            "base": {"start_date": "2024-01-02", "end_date": "2024-01-04", "frequency": frequency,
                     "accounts": {"stock": spec["parameters"]["initial_cash"]}, "run_type": "b",
                     "rqdatac_uri": "disabled", "auto_update_bundle": False,
                     "data_bundle_path": str(root / "ABSENT_BUNDLE"), "persist": True,
                     "persist_mode": "real_time", "capital_gain_tax_rate": 0},
            "extra": {"log_level": "error", "locale": "en_US"},
            "mod": {"sys_progress": {"enabled": False}, "sys_analyser": {"enabled": False},
                    "sys_simulation": {"matching_type": "current_bar", "volume_percent": .25},
                    "probe": {"enabled": True, "lib": "ashare_quant.rq_mod", "priority": 1000,
                              "scenario": args.scenario, "actions": args.scenario == "corporate_action",
                              "missing": [], "tplus": 1, "recorded": True, "result": str(result),
                              "events_path": str(events_path), "checkpoint": str(checkpoint),
                              "publication": str(publication), "identity": "",
                              "host_source_sha256": source_sha256(),
                              "host_account": args.account,
                              "host_run_id": args.run_id, "host_manifest_sha256": args.manifest_sha256,
                              "invocation_id": "<transport-token>",
                              "host_runtime": runtime_fingerprint()}},
        }
        config_module.user_config = lambda: {}
        config_module.project_config = lambda: {}
        config = config_module.parse_config(requested, source_code=source)
        # Physical publication location is not part of semantic identity, matching rc2 path policy.
        actual_publication = config.mod.probe.publication
        config.mod.probe.publication = "<artifact-path>"
        identity = checkpoint_identity(config, source, records, __file__)
        config.mod.probe.publication = actual_publication
        config.mod.probe.identity = identity
        config.mod.probe.invocation_id = args.invocation_id
        payload = AtomicFile(checkpoint, identity).read()
        if args.reconcile_only:
            receipt = (payload["receipt"] if payload["complete"] else payload["host_publication"]["receipt"]) if payload else {
                "engine": "RQAlpha", "evidence_kind": "synthetic", "account": None, "engine_events": [],
                "real_market_validation": "NOT_RUN"}
            atomic_json(publication, {"identity": identity, "cursor": payload["host_publication"]["cursor"] if payload else 0,
                "complete": bool(payload and payload["complete"]), "checkpoint_present": payload is not None,
                "receipt": receipt, "invocation_id": args.invocation_id})
            return  # No engine initialization, strategy callback, input replay or checkpoint write.
        if payload:
            atomic_json(publication, payload["host_publication"] | {"invocation_id": args.invocation_id})
            if payload["complete"]:
                if not isinstance(payload["receipt"], dict):
                    raise RuntimeError("COMPLETED_RECEIPT_MISSING")
                atomic_json(result, payload["receipt"])
                return
        Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)

        def forbidden(*_, **__):
            raise RuntimeError("DEFAULT_BUNDLE_DISABLED")

        rqalpha.main.BaseDataSource = forbidden
        rqalpha.main.PersistHelper = StrictHelper
        rqalpha.main.run(config, source_code=source)
        # Native run may return without producing a completed receipt on an exception.
        final = AtomicFile(checkpoint, identity).read()
        if not final or not final["complete"] or not isinstance(final["receipt"], dict):
            raise RuntimeError("NATIVE_RUN_NOT_COMPLETED")
        atomic_json(result, final["receipt"])


if __name__ == "__main__":
    run()
