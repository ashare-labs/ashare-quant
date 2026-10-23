"""Fixed-origin, bounded HTTP client. No filesystem, SQL, shell or arbitrary routes."""

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

PREFIXES = {
    "python_minute": "/api/v1/python",
    "python_daily": "/api/v1/python",
    "factors": "/api/v1/factors",
    "portfolio": "/api/v1/portfolio",
    "daily": "/api/v1/tasks/daily",
}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


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

    async def request(self, method, path, *, body=None, params=None):
        # All callers build these paths from constants and validated identifiers.
        async with httpx.AsyncClient(
            base_url=self.settings.backend_url,
            timeout=self.settings.timeout,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            try:
                async with client.stream(method, path, json=body, params=params) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise ResearchError(
                                "BACKEND_RESPONSE_LIMIT",
                                "Response exceeded 32 MiB; narrow the query or use backend export",
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
                        raise ResearchError(
                            str(error.get("code", "BACKEND_HTTP_ERROR"))[:120],
                            str(error.get("message", "Backend rejected the request"))[:2000],
                            status=response.status_code,
                            retry_after=response.headers.get("retry-after"),
                            remedy="Check capabilities, request fields and backend task logs; "
                            "keep the same idempotency key when retrying a submission",
                        )
                    return value
            except httpx.TransportError as exc:
                raise ResearchError(
                    "BACKEND_UNREACHABLE",
                    "Backend connection failed; a submitted task may already exist",
                    failure=type(exc).__name__,
                ) from exc

    async def optional(self, path):
        try:
            return {"available": True, "value": await self.request("GET", path)}
        except ResearchError as exc:
            return {"available": False, "error": exc.payload}
