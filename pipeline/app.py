"""ASGI application for the Temporal-based document OCR pipeline."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import db
from .auth.config import load_auth_config, validate_auth_config
from .rate_limit import limiter
from .services import tenants
from .temporal import client as temporal_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration and initialise the local database on startup.

    Temporal and MinIO are NOT connected here: they are connected on first use
    (see temporal.client / storage.minio) so the API can start, serve
    /health and every SQLite-only route, and be exercised by TestClient without
    those backends being up.
    """
    auth_cfg = load_auth_config()
    validate_auth_config(auth_cfg)
    if auth_cfg.disabled:
        logging.warning(
            "WARNING: AUTH_DISABLED=true — every caller is treated as synthetic "
            "master_admin with unrestricted instance access. Do not expose this "
            "API beyond the internal network or set AUTH_DISABLED=false until "
            "the maintainer UI sends Bearer tokens."
        )
    else:
        logging.info(
            "Auth enabled: issuer=%s audience=%s jwks=%s",
            auth_cfg.keycloak_issuer,
            auth_cfg.keycloak_audience or "(none)",
            auth_cfg.keycloak_jwks_url,
        )

    # Initialize SQLite database
    print("Initializing SQLite database...")
    db.init_db()

    # Self-heal the tenant registry: backfill a ``tenants`` row for every tenant
    # that already exists de-facto (documents / index registry / Keycloak org)
    # but was never inserted via POST /tenants. Best-effort — a failure here
    # (e.g. Keycloak not reachable yet) must never block startup; the local
    # reconcile from documents + indexes still runs regardless.
    try:
        reconciled = tenants.reconcile_tenants(include_keycloak=True)
        print(f"Tenant registry reconciled: {len(reconciled)} tenant(s) registered")
    except Exception as exc:  # noqa: BLE001 - startup must not fail on reconcile
        logging.warning("Startup tenant reconcile failed (non-fatal): %s", exc)

    # Temporal / MinIO are connected lazily. Still surface obviously-broken
    # storage config at startup as a warning so a misconfigured deployment is
    # visible in the logs before the first upload fails.
    logging.info("Temporal host (connected on first use): %s", temporal_client.host())
    if not os.environ.get("MINIO_ACCESS_KEY") or not os.environ.get("MINIO_SECRET_KEY"):
        logging.warning(
            "MINIO_ACCESS_KEY / MINIO_SECRET_KEY are not set — any route that "
            "touches object storage will fail until they are configured."
        )

    # Fail fast on chunking misconfig (no silent Gemma/deterministic fallback).
    from .config import ConfigurationError, validate_environment

    chunking_errors = [e for e in validate_environment() if e.startswith("CHUNKING_")]
    if chunking_errors:
        raise ConfigurationError(
            "Invalid chunking configuration:\n" + "\n".join(f"  - {e}" for e in chunking_errors)
        )

    yield
    # Cleanup if needed


app = FastAPI(
    title="Document Ingestion Pipeline API",
    description="""
REST API for the Temporal-based document OCR pipeline with translation support.

## Workflow Stages

1. `registered` - Document registered
2. `ocr_processing` - OCR in progress
3. `ocr_review` - **Waiting for OCR review/approval**
4. `translation_processing` - Translating non-English content
5. `translation_review` - **Waiting for translation review/approval**
6. `chunking` - Chunking in progress
7. `chunk_review` - **Waiting for chunk review/approval**
8. `ready_for_ingestion` - **Waiting for final approval**
9. `ingesting` - Ingesting to Marqo
10. `completed` - Done
11. `failed` - Error occurred

## Review Flow

1. Start workflow with `POST /upload` or `POST /documents`
2. Wait for `ocr_review` stage
3. Review/edit pages with `GET/PATCH /documents/{id}/pages/{num}`
4. Approve with `POST /documents/{id}/approve-ocr`
5. Wait for `translation_review` stage
6. Review/edit translations with `PATCH /documents/{id}/pages/{num}`
7. Approve with `POST /documents/{id}/approve-translation`
8. Wait for `chunk_review` stage
9. Review/edit chunks with `GET/PATCH /documents/{id}/chunks/{num}`
10. Approve with `POST /documents/{id}/approve-chunks`
11. Wait for `ready_for_ingestion` stage
12. Final approval with `POST /documents/{id}/approve-ingestion`
13. Workflow completes automatically
    """,
    version="2.0.0",
    lifespan=lifespan
)

# CORS configuration - explicit origins for security
ALLOWED_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

__all__ = ["app", "lifespan", "limiter"]

from .routers import (  # noqa: E402
    admin,
    content,
    documents,
    documents_actions,
    search,
    tenants,
)

# Registration order is match order (Starlette is first-registered-wins), so
# `documents` must precede `documents_actions`: GET/DELETE /documents/{workflow_id}
# is registered before POST /documents/reconcile, as in the single-file version.
#
# The routes are spliced in rather than `include_router`-ed because FastAPI 0.141
# makes `include_router` lazy — it appends an opaque `_IncludedRouter` placeholder,
# so `app.routes` stops being a flat list of `APIRoute` that callers can introspect.
for _router in (
    documents.router,
    documents_actions.router,
    content.router,
    search.router,
    tenants.router,
    admin.router,
):
    app.router.routes.extend(_router.routes)
app.router._mark_routes_changed()
