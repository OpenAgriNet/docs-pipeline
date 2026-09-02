"""Ephemeral live-progress ticker.

SQLite remains canonical for stage edges and finished page/chunk rows.
This cache is the hot counter the UI polls during a running stage.

Uses Redis when ``REDIS_URL`` is set (required for API + worker in compose).
Falls back to a process-local dict for tests.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

_TRUE_TTL_DEFAULT = 600
_KEY_PREFIX = "pipeline:progress:"

_memory_lock = threading.Lock()
_memory: dict[str, tuple[float, dict]] = {}
_redis_client: Any = None
_redis_failed = False


def _ttl_seconds() -> int:
    raw = os.environ.get("PROGRESS_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return _TRUE_TTL_DEFAULT
    try:
        return max(30, int(raw))
    except ValueError:
        return _TRUE_TTL_DEFAULT


def _key(workflow_id: str) -> str:
    return f"{_KEY_PREFIX}{workflow_id}"


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "").strip()


def reset_for_tests() -> None:
    """Drop in-memory entries and the Redis client (pytest)."""
    global _redis_client, _redis_failed
    with _memory_lock:
        _memory.clear()
    _redis_client = None
    _redis_failed = False


def _get_redis() -> Any:
    global _redis_client, _redis_failed
    if _redis_failed:
        return None
    if _redis_client is not None:
        return _redis_client
    url = redis_url()
    if not url:
        return None
    try:
        import redis

        _redis_client = redis.Redis.from_url(url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception:
        logging.warning("Live progress Redis unavailable at %s", url, exc_info=True)
        _redis_failed = True
        _redis_client = None
        return None


def put(workflow_id: str, payload: dict) -> None:
    if not workflow_id or not isinstance(payload, dict):
        return
    record = dict(payload)
    expires = time.monotonic() + _ttl_seconds()
    client = _get_redis()
    if client is not None:
        try:
            client.set(_key(workflow_id), json.dumps(record), ex=_ttl_seconds())
            return
        except Exception:
            logging.debug("Live progress Redis SET failed", exc_info=True)
    with _memory_lock:
        _memory[workflow_id] = (expires, record)


def get(workflow_id: str) -> Optional[dict]:
    if not workflow_id:
        return None
    client = _get_redis()
    if client is not None:
        try:
            raw = client.get(_key(workflow_id))
            if not raw:
                return None
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            logging.debug("Live progress Redis GET failed", exc_info=True)
            return None
    now = time.monotonic()
    with _memory_lock:
        hit = _memory.get(workflow_id)
        if not hit:
            return None
        expires, record = hit
        if expires <= now:
            _memory.pop(workflow_id, None)
            return None
        return dict(record)


def clear(workflow_id: str) -> None:
    if not workflow_id:
        return
    client = _get_redis()
    if client is not None:
        try:
            client.delete(_key(workflow_id))
        except Exception:
            logging.debug("Live progress Redis DEL failed", exc_info=True)
    with _memory_lock:
        _memory.pop(workflow_id, None)
