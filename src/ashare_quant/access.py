"""Explicit additional browser origins for an operator-authorized private entry."""

from urllib.parse import urlsplit


def configured_origins(origins):
    """Fail closed on broad or non-origin values; never infer trust from a request."""
    result = frozenset(origins)
    for origin in result:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(c in parsed.netloc for c in "*\\ \t\r\n")
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or parsed.port == 0
            or origin != f"{parsed.scheme}://{parsed.netloc}"
        ):
            raise ValueError("Configure exact http(s) origins without wildcards or paths")
    return result


def trusted_browser_request(request):
    origin = request.headers.get("origin")
    if not origin:
        return request.headers.get("sec-fetch-site") != "cross-site"
    if origin in getattr(request.app.state, "trusted_browser_origins", ()):
        return True
    try:
        parsed = urlsplit(origin)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and not parsed.username and not parsed.password
            and parsed.path == "" and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False
