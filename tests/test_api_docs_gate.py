"""API docs exposure gate (OpenAPI / Swagger must not leak by default in compose)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.api_docs import api_docs_routes, env_flag


def test_env_flag_parsing(monkeypatch):
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    assert env_flag("API_DOCS_ENABLED", default=True) is True
    assert env_flag("API_DOCS_ENABLED", default=False) is False
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    assert env_flag("API_DOCS_ENABLED", default=True) is False
    monkeypatch.setenv("API_DOCS_ENABLED", "true")
    assert env_flag("API_DOCS_ENABLED", default=False) is True


def test_api_docs_routes_disabled_hides_schema():
    app = FastAPI(**api_docs_routes(False))
    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_api_docs_routes_enabled_exposes_schema():
    app = FastAPI(**api_docs_routes(True))
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
