"""Separate local backend surface; no front-end or system scheduler ownership."""

from datetime import date

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ashare_quant.contracts import DomainError

from .contracts import (
    AdvanceAction,
    CloseInput,
    ControlAction,
    Decision,
    ForkSimulation,
    SessionConfig,
)
from .errors import error_value
from .risk import require
from .service import PaperService


def create_app(root):
    app = FastAPI(title="AQ local daily simulation and research replay", version="paper-api.v2")
    service = PaperService(root)
    app.state.paper = service

    @app.middleware("http")
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={"error": error_value(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "PAPER_INVALID_REQUEST",
                    "message": "请求字段不符合冻结契约。",
                    "detail": "; ".join(str(e["loc"]) + ": " + e["msg"] for e in exc.errors()),
                    "retryable": False,
                    "next_action": "按 OpenAPI 检查输入；不支持真实下单模式。",
                }
            },
        )

    @app.get("/api/v1/paper/capabilities")
    def capabilities():
        return {
            "contract_version": "paper-api.v2",
            "native_continuation": True,
            "data_modes": ["synthetic_fixture", "historical_research"],
            "historical_replay_enabled": True,
            "historical_pit_verified": False,
            "execution_permission": False,
            "source_sha256": service_source(),
            "observed_data_enabled": False,
            "continuous_paper_accepted": False,
            "broker_enabled": False,
            "strategy_execution": "pinned_shared_daily_signals",
            "awaiting": [
                "AD_execution_bundle",
                "final_strategy_reference_handoff",
                "corporate_action_continuation",
                "frozen_real_risk_policy",
                "real_end_to_end",
                "quant_web_acceptance",
            ],
        }

    def service_source():
        from ashare_quant.identity import source_sha256

        return source_sha256()

    @app.get("/api/v1/paper/schema")
    def schema():
        return app.openapi()

    @app.get("/api/v1/paper/sessions")
    def catalogue():
        return service.catalogue()

    @app.get("/api/v1/paper/risk-profiles")
    def risk_profiles():
        from .risk import profiles

        return profiles()

    @app.post("/api/v1/paper/sessions/{sid}/fork", status_code=201)
    def fork(sid: str, body: ForkSimulation):
        from .replay import bind, load

        _, config = service.session(sid)
        binding, _ = load(service, sid, config)
        directory = service.root / sid / "replay"
        return bind(
            service,
            directory / "manifest.json",
            directory / "input.json",
            config.strategy_id,
            binding["window"]["id"],
            body.account_id,
            accept_model=binding["accept_owner_state_model"],
            risk=body.risk.model_dump(mode="json"),
        )

    @app.post("/api/v1/paper/sessions", status_code=201)
    def create(body: SessionConfig):
        row = service.create(body)
        return {
            "session_id": row["id"],
            "identity": row["identity"],
            "data_mode": body.data_mode,
        }

    @app.get("/api/v1/paper/sessions/{sid}")
    def status(sid: str):
        return service.status(sid)

    @app.get("/api/v1/paper/sessions/{sid}/view")
    def view(sid: str, day: date | None = None):
        return service.view(sid, day.isoformat() if day else None)

    @app.post("/api/v1/paper/sessions/{sid}/advance")
    def advance(sid: str, body: AdvanceAction):
        from .replay import advance as replay_advance

        return replay_advance(service, sid, body.day.isoformat(), body.expected_parent)

    @app.post("/api/v1/paper/sessions/{sid}/days/{day}/work")
    def work(sid: str, day: date, body: ControlAction):
        frozen = Decision.model_validate_json(service.day(sid, day.isoformat())["decision"])
        require(frozen.expected_parent == body.expected_parent, "PAPER_PARENT_CONFLICT")
        return service.work(sid, day.isoformat())

    @app.post("/api/v1/paper/sessions/{sid}/decisions", status_code=201)
    def decision(sid: str, body: Decision):
        row = service.prepare(sid, body)
        return {"day": row["day"], "status": row["status"], "intent_id": row["intent_id"]}

    @app.post("/api/v1/paper/sessions/{sid}/closes", status_code=202)
    def close(sid: str, body: CloseInput):
        row = service.seal_close(sid, body)
        return {"day": row["day"], "status": row["status"], "input_id": row["input_id"]}

    @app.get("/api/v1/paper/sessions/{sid}/days/{day}/report")
    def report(sid: str, day: date):
        return service.report(sid, day.isoformat())

    @app.post("/api/v1/paper/sessions/{sid}/pause")
    def pause(sid: str, body: ControlAction):
        return service.control(sid, "pause", body.expected_parent, body.reason)

    @app.post("/api/v1/paper/sessions/{sid}/resume")
    def resume(sid: str, body: ControlAction):
        return service.control(sid, "resume", body.expected_parent, body.reason)

    return app
