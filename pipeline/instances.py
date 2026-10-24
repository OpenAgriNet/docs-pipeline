"""Tenant (instance) identifiers: the default tenant and the canonical form."""

import os


def default_instance() -> str:
    """Fallback instance id for new docs and legacy rows without a value."""
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def normalize_instance(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text or default_instance()
