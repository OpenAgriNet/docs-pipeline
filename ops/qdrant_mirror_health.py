"""Fail closed if the authoritative-source reconciliation is unhealthy or stale."""
import json
import time
from pathlib import Path


def mirror_health(path, collection, max_age=120, now=None):
    now = time.time() if now is None else now
    try:
        status = json.loads(Path(path).read_text())
        age = now - status["last_success_at"]
        valid = (status.get("ok") is True and status.get("collection") == collection
                 and status["payloads_verified"] == status["source_points"]
                 and status["target_points"] >= status["source_points"] and -5 <= age <= max_age)
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("Qdrant source synchronization is unavailable") from None
    if not valid:
        raise RuntimeError("Qdrant source synchronization is unhealthy or stale")
    return {"last_success_at": status["last_success_at"], "age_seconds": round(age, 2),
            "source_points": status["source_points"], "pending_deletes": status.get("pending_deletes", 0)}
