"""Central security policy for EVKIN's local HTTP APIs."""
from __future__ import annotations

import os
import secrets
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


API_KEY_HEADER = "X-EVKIN-API-Key"
PRIMARY_API_KEY_ENV = "EVKIN_API_KEY"
LEGACY_API_KEY_ENV = "EVKIN_ANALYTICS_API_KEY"
INSECURE_DEV_ENV = "EVKIN_ALLOW_INSECURE_LOCAL_DEV"
ALLOWED_ORIGINS_ENV = "EVKIN_ALLOWED_ORIGINS"

DEFAULT_PUBLIC_PATHS = frozenset({
    "/",
    "/index.html",
    "/health",
    "/api/health",
    "/api/ping",
})
DEFAULT_PUBLIC_PREFIXES = ("/assets/",)


def insecure_local_dev_enabled() -> bool:
    return os.environ.get(INSECURE_DEV_ENV, "").strip() == "1"


def configured_api_key() -> str | None:
    """Return the primary key, with the old analytics key as a migration fallback."""
    primary = os.environ.get(PRIMARY_API_KEY_ENV, "").strip()
    if primary:
        return primary
    legacy = os.environ.get(LEGACY_API_KEY_ENV, "").strip()
    return legacy or None


def require_api_key(request: Request) -> None:
    """FastAPI dependency that rejects missing/mismatched local API tokens."""
    if insecure_local_dev_enabled():
        return
    expected = configured_api_key()
    if not expected:
        raise HTTPException(status_code=503, detail="EVKIN API security is not configured")
    supplied = request.headers.get(API_KEY_HEADER, "")
    if not supplied:
        supplied = request.cookies.get("evkin_local_api_key", "")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Protect every non-public route, including routes mounted by sub-apps."""

    def __init__(
        self,
        app,
        public_paths: Iterable[str] = DEFAULT_PUBLIC_PATHS,
        public_prefixes: Iterable[str] = DEFAULT_PUBLIC_PREFIXES,
    ):
        super().__init__(app)
        self.public_paths = frozenset(public_paths)
        self.public_prefixes = tuple(public_prefixes)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        is_public = path in self.public_paths or any(path.startswith(prefix) for prefix in self.public_prefixes)
        if request.method == "OPTIONS" or is_public:
            return await call_next(request)
        try:
            require_api_key(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)


def allowed_origins() -> list[str]:
    """Return an explicit CORS allowlist; wildcard origins are never accepted."""
    configured = os.environ.get(ALLOWED_ORIGINS_ENV, "").strip()
    if configured:
        values = [item.strip().rstrip("/") for item in configured.split(",") if item.strip()]
    else:
        port = os.environ.get("EVKIN_HTTP_PORT", "8000").strip() or "8000"
        values = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    result: list[str] = []
    for origin in values:
        if origin == "*":
            raise RuntimeError("Wildcard CORS origin is not allowed")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise RuntimeError("EVKIN_ALLOWED_ORIGINS contains an invalid origin")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise RuntimeError("EVKIN_ALLOWED_ORIGINS contains an invalid origin")
        if origin not in result:
            result.append(origin)
    if not result:
        raise RuntimeError("EVKIN_ALLOWED_ORIGINS must contain at least one origin")
    return result


CORS_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
CORS_HEADERS = ["Accept", "Content-Type", API_KEY_HEADER]
