"""Fixed-origin, bounded HTTP client. No filesystem, SQL, shell or arbitrary routes."""

import json
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

PREFIXES = {
    "python_minute": "/api/v1/python",
    "python_daily": "/api/v1/python",
    "factors": "/api/v1/factors",
    "portfolio": "/api/v1/history",
    "daily": "/api/v1/history",
}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
CACHE_CAPACITY = 4


def _error_hint(code: str, status: int, method: str) -> str:
    if code == "M2_JOB_RESULT_NOT_READY":
        return "Run has not produced a published result yet; call get_run with wait_seconds>0 or inspect failure diagnostics."
    if code in {"PYTHON_SYNTAX_ERROR", "AQUANT_LEGACY_IMPORT", "AQUANT_INITIALIZE_REQUIRED"}:
        return "Call check_strategy first to validate syntax, aquant imports, and lifecycle functions."
    if code in {"M2_JOB_NOT_FOUND", "HISTORY_NOT_FOUND"} or status == 404:
        return "Verify run_id via list_runs() or pass idempotency_key to get_run()."
    if status in {429, 503}:
        return "Backend is temporarily busy or unavailable; wait briefly and retry."
    if method == "POST":
        return "Inspect the message/details; if retrying an identical submission after a timeout, reuse the same idempotency_key."
    return "Inspect error code and message, or call get_environment() to verify available capabilities."


class ResearchError(Exception):
    def __init__(self, code, message, **details):
        self.payload = dict(code=code, message=message, **details)
        super().__init__(json.dumps(self.payload, ensure_ascii=False))


@dataclass(frozen=True)
class Settings:
    backend_url: str
    timeout: float = 15.0

    def __post_init__(self):
        parsed = urlsplit(self.backend_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("Backend must be an explicit loopback HTTP origin with a port")
        if not 0 < self.timeout <= 120:
            raise ValueError("Timeout must be in (0, 120] seconds")


class Backend:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport
        self._cache: OrderedDict[tuple, dict] = OrderedDict()

    async def request(self, method, path, *, body=None, params=None, headers=None):
        # All callers build these paths from constants and validated identifiers.
        async with httpx.AsyncClient(
            base_url=self.settings.backend_url,
            timeout=self.settings.timeout,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            try:
                async with client.stream(
                    method, path, json=body, params=params, headers=headers
                ) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise ResearchError(
                                "BACKEND_RESPONSE_LIMIT",
                                "Response exceeded 32 MiB; narrow the query or use smaller date windows",
                            )
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError) as exc:
                        raise ResearchError(
                            "BACKEND_PROTOCOL",
                            "Backend did not return JSON",
                            status=response.status_code,
                        ) from exc
                    if not isinstance(value, dict):
                        raise ResearchError(
                            "BACKEND_PROTOCOL", "Expected a JSON object from the backend"
                        )
                    if response.status_code >= 300:
                        error = value.get("error")
                        if not isinstance(error, dict):
                            error = {}
                        code = str(error.get("code", "BACKEND_HTTP_ERROR"))[:120]
                        message = str(error.get("message", "Backend rejected the request"))[:2000]
                        raise ResearchError(
                            code,
                            message,
                            status=response.status_code,
                            retryable=response.status_code in {429, 502, 503, 504},
                            retry_after=response.headers.get("retry-after"),
                            hint=_error_hint(code, response.status_code, method),
                        )
                    return value
            except httpx.TransportError as exc:
                raise ResearchError(
                    "BACKEND_UNREACHABLE",
                    "Backend connection failed; if a submission was in flight, check get_run(idempotency_key=...) before retrying",
                    failure=type(exc).__name__,
                    retryable=True,
                ) from exc

    async def cached_get(self, cache_key: tuple, path: str, *, params=None):
        if cache_key and cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]
        value = await self.request("GET", path, params=params)
        if cache_key:
            self._cache[cache_key] = value
            while len(self._cache) > CACHE_CAPACITY:
                self._cache.popitem(last=False)
        return value

    async def optional(self, path, *, params=None):
        try:
            return {"available": True, "value": await self.request("GET", path, params=params)}
        except ResearchError as exc:
            return {"available": False, "error": exc.payload}
