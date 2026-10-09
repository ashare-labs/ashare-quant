"""Two GET-only routes, no query input, execution or artifact uploads."""

from fastapi import Request, Response

from ashare_quant.contracts import DomainError, ErrorResponse
from ashare_quant.m2.common import digest

from .contracts import M2ResultDetail, M2ResultList
from .service import list_results, result_detail

PREFIX = "/api/v1/m2/results"


def mount(app):
    # Applies equally to validation and DomainError responses; the frontend must
    # remove old success data after any failing response, including If-None-Match.
    @app.middleware("http")
    async def no_stale_m2_results(request, call_next):
        response = await call_next(request)
        if request.url.path == PREFIX or request.url.path.startswith(PREFIX + "/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def query(request):
        if request.query_params:
            raise DomainError(
                "M2_RESULT_QUERY_NOT_SUPPORTED", "Only a sealed result ID is accepted", 422
            )

    @app.get(PREFIX, responses={503: {"model": ErrorResponse}})
    def m2_result_list(request: Request, response: Response) -> M2ResultList:
        query(request)
        result = list_results()
        response.headers["ETag"] = '"' + digest(result) + '"'
        return result

    @app.get(PREFIX + "/{result_id}", responses={503: {"model": ErrorResponse}})
    def m2_result_detail(result_id: str, request: Request, response: Response) -> M2ResultDetail:
        query(request)
        result = result_detail(result_id)
        response.headers["ETag"] = '"' + digest(result) + '"'
        return result
