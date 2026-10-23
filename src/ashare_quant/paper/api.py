"""Separate local backend surface; no front-end or system scheduler ownership."""

from datetime import date

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ashare_quant.contracts import DomainError
from ashare_quant.paper.errors import error_value
from ashare_quant.paper.service import PaperService


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
        return {"contract_version": "paper-history.v1", "read_only": True,
                "source_sha256": service_source(), "execution_permission": False}

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
        from ashare_quant.paper.risk import profiles

        return profiles()




    @app.get("/api/v1/paper/sessions/{sid}")
    def status(sid: str):
        return service.status(sid)

    @app.get("/api/v1/paper/sessions/{sid}/view")
    def view(sid: str, day: date | None = None):
        return service.view(sid, day.isoformat() if day else None)





    @app.get("/api/v1/paper/sessions/{sid}/days/{day}/report")
    def report(sid: str, day: date):
        return service.report(sid, day.isoformat())



    return app
