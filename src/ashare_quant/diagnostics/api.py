from ashare_quant.diagnostics.contracts import (
    DiagnosticCapabilities,
    DiagnosticQuery,
    DiagnosticResult,
)
from ashare_quant.diagnostics.service import Diagnostics


def mount(app, configuration=None):
    service = Diagnostics(configuration)
    app.state.diagnostics = service

    @app.get("/api/v1/source-diagnostics/capabilities")
    def capabilities() -> DiagnosticCapabilities:
        return service.capabilities()

    @app.post("/api/v1/source-diagnostics")
    def diagnose(request: DiagnosticQuery) -> DiagnosticResult:
        return service.diagnose(request)
