"""Separate local backend surface; no front-end or system scheduler ownership."""

from datetime import date

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ashare_quant.contracts import DomainError

from .contracts import CloseInput, ControlAction, Decision, SessionConfig
from .service import PaperService


def create_app(root):
    app = FastAPI(title="AQ daily continuation foundations", version="paper-api.v1")
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
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": False,
                    "next_action": "Read session alerts and frozen evidence before resuming.",
                }
            },
        )

    @app.get("/api/v1/paper/capabilities")
    def capabilities():
        return {
            "contract_version": "paper-api.v1",
            "native_continuation": True,
            "data_modes": ["synthetic_fixture"],
            "observed_data_enabled": False,
            "continuous_paper_accepted": False,
            "broker_enabled": False,
            "strategy_execution": "external_frozen_intents_only",
            "awaiting": [
                "shared_strategy_receipts",
                "AD_execution_bundle",
                "real_end_to_end",
                "quant_web_acceptance",
            ],
        }

    @app.post("/api/v1/paper/sessions", status_code=201)
    def create(body: SessionConfig):
        row = service.create(body)
        return {
            "session_id": row["id"],
            "identity": row["identity"],
            "data_mode": "synthetic_fixture",
        }

    @app.get("/api/v1/paper/sessions/{sid}")
    def status(sid: str):
        return service.status(sid)

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
