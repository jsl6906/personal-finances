"""HTTP hardening: security headers on every response and a same-origin check for state-changing API calls."""

from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; script-src 'self'; connect-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
    "frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
# Swagger UI loads its assets from a CDN.
DOCS_CSP = "frame-ancestors 'none'; object-src 'none'"
HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return None


def cross_site(scope: Scope) -> bool:
    """True when a browser tells us the request came from another site."""
    if _header(scope, b"sec-fetch-site") == "cross-site":
        return True
    origin = _header(scope, b"origin")
    if not origin or origin == "null":
        return origin == "null"
    host = _header(scope, b"x-forwarded-host") or _header(scope, b"host")
    return urlsplit(origin).netloc != host


class SecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if scope["method"] in UNSAFE_METHODS and path.startswith("/api/") and cross_site(scope):
            await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"detail":"Cross-site request blocked"}'})
            return
        csp = (DOCS_CSP if path.startswith("/api/docs") else CSP).encode()

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in HEADERS if k not in present]
                if b"content-security-policy" not in present:
                    headers.append((b"content-security-policy", csp))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
