"""
ASGI entrypoint for the API plane.

``pipeline.app:app`` is the name the Dockerfile, docker-compose and any external
process manager should target when serving the API. It exists so that the
serving contract is a stable, deliberately chosen name rather than an
implementation detail of whichever module happens to construct the app today.

Right now this module is a **pure re-export**: the ``FastAPI`` object, its
lifespan, its middleware and all of its routes are still defined in
``pipeline.api``. Nothing was moved. That is intentional — the point of this
module is to establish the entrypoint name first, so that a later change can
relocate app construction, middleware or routers behind it without having to
touch the Dockerfile, compose files or deploy tooling again.

``pipeline.api:app`` therefore keeps working unchanged, and
``pipeline.app.app is pipeline.api.app`` is true. Tests and tooling that import
``pipeline.api`` directly (including ones that monkeypatch module attributes or
call route handlers as plain functions) are unaffected.
"""

from pipeline.api import app

__all__ = ["app"]
