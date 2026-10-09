"""Called only by the sealed -I -S fresh-interpreter launcher."""
import os
import sys
from pathlib import Path


def execute(request, release, package_root):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("FRESH_ISOLATED_INTERPRETER_REQUIRED")
    if sys.gettrace() is not None or sys.getprofile() is not None:
        raise RuntimeError("EXTERNAL_MONITORING_FORBIDDEN")
    if sys.monitoring.get_tool(4) is not None:
        raise RuntimeError("MONITORING_SLOT_4_MUST_BE_UNUSED")
    from rqprobe.guards import install_network_guard
    install_network_guard()
    from ashare_quant.identity import source_inventory
    from ashare_quant.runtime import runtime_fingerprint
    from rqprobe.runtime import runtime_identity
    if source_inventory() != release["sources"]:
        raise RuntimeError("INSTALLED_BACKEND_IDENTITY_MISMATCH")
    runtime = runtime_fingerprint()
    rq = runtime_identity()
    from ashare_quant.d1_boundary import fixed_records
    from ashare_quant.d1_codec import public_digest
    from ashare_quant.d1_listing import ListingOwner
    from ashare_quant.rq_mod import atomic_json

    from .config import fixed_config
    from .state import CompletedReceipt, session

    root, store = Path(request["root"]), Path(request["store"])
    owner = ListingOwner(store)
    records = fixed_records(owner.contract)
    source = Path(__file__).with_name("strategy.py").read_text()
    run_id = public_digest({"release_sha256": request["release_sha256"],
                            "case": request["case"], "records": records,
                            "profile": request["accept_profile"],
                            "listing_contract": owner.contract_digest})
    config, requested = fixed_config(root, store, run_id, request["release_sha256"], source)
    manifest = {"schema": 1, "run_identity": run_id, "release_sha256": request["release_sha256"],
                "release": release, "runtime": runtime, "rq": rq,
                "binding": owner.snapshot.report.model_dump(mode="json"),
                "records": records, "requested_native_config": requested,
                "pid": os.getpid(), "parent_pid": os.getppid(), "new_interpreter": True,
                "isolated": True, "no_site": True, "package_root": str(package_root),
                "invocation": request["invocation"], "fault": request["fault"],
                "strict_d1": "BLOCKED", "paper": "BLOCKED", "general_api_execution": "BLOCKED"}
    manifest_path = root / (request["invocation"] + ".manifest.json")
    atomic_json(manifest_path, manifest)
    if request["check_only"]:
        atomic_json(root / (request["invocation"] + ".completion.json"),
                    {"status": "PREFLIGHT_ONLY", "native_run": False, "account_created": False})
        return
    atomic_json(root / "inputs.json", records)
    session.update(invocation=request["invocation"])
    import rqalpha.main as native
    from rqalpha.environment import Environment
    from rqalpha.model.order import Order
    from rqalpha.model.trade import Trade

    from rqprobe.checkpoint import AtomicFile, Counter

    from .checkpoint import FixedHelper

    Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)
    def forbidden(*args, **kwargs):
        raise RuntimeError("DEFAULT_BUNDLE_DISABLED")
    native.BaseDataSource = forbidden
    native.PersistHelper = FixedHelper
    if request["fault"] != "none":
        os.environ["PROBE_KILL"] = request["fault"]
    status = "FAILED"
    try:
        try:
            native.run(config, source_code=source)
        except CompletedReceipt as done:
            receipt, resumed = done.receipt, True
        else:
            payload = AtomicFile(root / "native.chk", session["identity"]).read()
            if not payload or not payload["complete"] or not isinstance(payload["receipt"], dict):
                raise RuntimeError("BR1_NATIVE_RUN_NOT_COMPLETED")
            receipt, resumed = payload["receipt"], False
        if receipt["identity"] != session["identity"]:
            raise RuntimeError("RECEIPT_IDENTITY_MISMATCH")
        atomic_json(root / "receipt.json", receipt)
        atomic_json(root / (request["invocation"] + ".completion.json"),
                    {"status": "COMPLETED", "receipt_reentry": resumed,
                     "identity": session["identity"], "runtime_epoch": session["runtime_epoch"]})
        status = "COMPLETED"
    except Exception as exc:
        error = getattr(exc, "error", None)
        cause = getattr(error, "exc_val", exc)
        from ashare_quant.contracts import DomainError
        details = cause.body() if isinstance(cause, DomainError) else {
            "type": type(cause).__name__, "message": str(cause)}
        atomic_json(root / (request["invocation"] + ".failure.json"),
                    {"failure": details, "identity": session.get("identity"),
                     "runtime_epoch": session.get("runtime_epoch")})
        raise
    finally:
        # Native run also cleans up in its finally; our host ownership closes even
        # if startup, restore, or native cleanup itself raised. SIGKILL is abrupt
        # process death and cannot promise Python finally execution.
        host = session.get("host")
        if host is not None:
            host.close()
            native.cleanup_resources(host.boundary.env)
        factory = session.get("proxy_factory")
        if factory is not None:
            factory.__exit__(None, None, None)
        manifest.update(identity=session.get("identity"),
                        effective_native_config=session.get("effective_config"),
                        runtime_epoch=session.get("runtime_epoch"), status=status)
        atomic_json(manifest_path, manifest)
        atomic_json(root / (request["invocation"] + ".teardown.json"),
                    {"status": status, "host_closed": host is None or host.boundary.life.reason is not None,
                     "native_singleton_cleared": Environment._env is None,
                     "pid": os.getpid(), "exit_after_return": True})
