"""Local production market endpoints over AD and the shared portfolio queue."""

from urllib.parse import urlsplit

from fastapi import Query, Request
from fastapi.responses import JSONResponse

from ashare_quant.contracts import DomainError
from ashare_quant.product.contracts import (
    ProductConfiguration,
    ProductRelease,
    ProductReleaseRequest,
    ProductSelection,
)
from ashare_quant.product.service import Product


def mount(app, store, configuration=None):
    product = Product(store, app.state.portfolio_jobs, configuration)
    app.state.product = product
    prefix = "/api/v1/product"

    @app.middleware("http")
    async def local_product(request, call_next):
        if request.url.path.startswith(prefix + "/"):
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                origin = request.headers.get("origin")
                try:
                    parsed = urlsplit(origin) if origin else None
                except ValueError:
                    parsed = None
                trusted = (
                    not origin and request.headers.get("sec-fetch-site") != "cross-site"
                ) or (
                    parsed and parsed.scheme in {"http", "https"}
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    and not parsed.username and not parsed.password
                    and parsed.path == "" and not parsed.query and not parsed.fragment
                )
                if not trusted:
                    return JSONResponse(status_code=403, content={"api_version": "v1", "error": {
                        "code": "PRODUCT_ORIGIN_REFUSED",
                        "message": "Use a trusted local client or loopback browser origin",
                    }}, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        if request.url.path.startswith(prefix + "/"):
            response.headers["Cache-Control"] = "no-store"
            if response.status_code in {429, 503}:
                response.headers["Retry-After"] = "2"
        return response

    def closed(request, allowed=()):
        if any(key not in allowed for key in request.query_params):
            raise DomainError("PRODUCT_QUERY_UNSUPPORTED", "Use the documented request fields", 422)

    @app.get(prefix + "/config")
    def configuration_view(request: Request) -> ProductConfiguration:
        closed(request)
        return product.describe()

    @app.get(prefix + "/catalogue")
    def catalogue(
        request: Request,
        query: str = Query(default="", max_length=100),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        closed(request, ("query", "limit", "offset"))
        return product.catalogue(query, limit, offset)

    @app.post(prefix + "/prices")
    def prices(body: ProductSelection, request: Request) -> dict:
        closed(request)
        return product.prices(body)

    @app.post(prefix + "/releases", status_code=201)
    def publish(body: ProductReleaseRequest, request: Request) -> ProductRelease:
        closed(request)
        return product.publish(body)
