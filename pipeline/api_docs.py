"""Helpers for gating FastAPI OpenAPI / Swagger exposure."""

from __future__ import annotations

import os


def env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def api_docs_routes(enabled: bool) -> dict[str, str | None]:
    """Return FastAPI docs/openapi kwargs. Disabled → no public schema surface."""
    if enabled:
        return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}
