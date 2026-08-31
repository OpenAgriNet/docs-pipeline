"""Live document progress for GET /runtime.

SQLite remains the write path for finished work. This module only *reads*:
a cheap SQLite snapshot plus the pending-activity heartbeat already returned
by Temporal ``describe()``. It does not write live counters and does not add
a second Temporal round-trip.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .. import db


_TRUE = {"1", "true", "yes", "on"}

LIVE_PROGRESS_STAGES = {
    "ocr_processing",
    "translation_processing",
    "chunking",
    "ingesting",
}

STAGE_TO_PHASE = {
    "ocr_processing": "ocr",
    "translation_processing": "translation",
    "chunking": "chunking",
    "ingesting": "ingest",
}

HEARTBEAT_STAGE_TO_PHASE = {
    "ocr": "ocr",
    "translation": "translation",
    "chunking": "chunking",
    "ingest": "ingest",
    "ingesting": "ingest",
}

PHASE_UNIT = {
    "ocr": "pages",
    "translation": "pages",
    "chunking": "pages",
    "ingest": "chunks",
}

# Match the activity heartbeat_timeout (10 minutes). OCR heartbeats once per
# persisted segment, which can take several minutes on large pages.
STALE_AFTER_SECONDS = 600.0
DESCRIBE_TTL_SECONDS = 2.0

_describe_cache: dict[str, tuple[float, Any]] = {}


def live_progress_enabled() -> bool:
    return os.environ.get("LIVE_PROGRESS_UI_ENABLED", "").strip().lower() in _TRUE


def clear_describe_cache() -> None:
    _describe_cache.clear()


def _prune_describe_cache(now: float) -> None:
    expired = [
        key for key, (ts, _) in _describe_cache.items() if now - ts >= DESCRIBE_TTL_SECONDS
    ]
    for key in expired:
        _describe_cache.pop(key, None)


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _phase_from_heartbeat(payload: dict) -> Optional[str]:
    phase = str(payload.get("phase") or "").strip().lower()
    if phase in PHASE_UNIT:
        return phase
    stage = str(payload.get("stage") or "").strip().lower()
    return HEARTBEAT_STAGE_TO_PHASE.get(stage)


def normalize_heartbeat(payload: Any) -> Optional[dict]:
    """Map OCR / chunking / translation / ingest heartbeat dicts to a common shape."""
    if not isinstance(payload, dict):
        return None
    phase = _phase_from_heartbeat(payload)
    if not phase:
        return None
    done = None
    for key in (
        "done",
        "pages_saved",
        "pages_processed",
        "pages_completed",
        "chunks_completed",
        "rows_seen",
        "records_ingested",
    ):
        if payload.get(key) is not None:
            done = _int_or_none(payload.get(key))
            break
    total = None
    for key in ("total", "total_pages", "pages_total", "chunks_total", "rows_total"):
        if payload.get(key) is not None:
            total = _int_or_none(payload.get(key))
            break
    if total is not None and total <= 0:
        total = None
    unit = str(payload.get("unit") or PHASE_UNIT[phase] or "pages")
    updated_at = payload.get("updated_at")
    if updated_at is not None:
        updated_at = str(updated_at)
    if done is None:
        done = 0
    return {
        "phase": phase,
        "done": done,
        "total": total,
        "unit": unit,
        "updated_at": updated_at,
    }


def sqlite_progress_snapshot(
    workflow_id: str,
    doc: dict,
    *,
    chunking_progress: Optional[dict] = None,
) -> Optional[dict]:
    """Cheap finished-work counters. Does not load page/chunk bodies."""
    stage = doc.get("stage")
    phase = STAGE_TO_PHASE.get(stage)
    if not phase:
        return None
    page_count = _int_or_none(doc.get("page_count")) or 0
    chunk_count = _int_or_none(doc.get("chunk_count")) or 0
    updated_at = doc.get("updated_at")
    if phase == "ocr":
        return {
            "phase": phase,
            "done": page_count,
            "total": None,
            "unit": "pages",
            "updated_at": updated_at,
        }
    if phase == "translation":
        return {
            "phase": phase,
            "done": db.count_translated_pages(workflow_id),
            "total": page_count or None,
            "unit": "pages",
            "updated_at": updated_at,
        }
    if phase == "chunking":
        if isinstance(chunking_progress, dict):
            done = _int_or_none(chunking_progress.get("pages_processed")) or 0
            total = _int_or_none(chunking_progress.get("pages_total"))
            return {
                "phase": phase,
                "done": done,
                "total": total if total and total > 0 else (page_count or None),
                "unit": "pages",
                "updated_at": chunking_progress.get("updated_at") or updated_at,
            }
        return {
            "phase": phase,
            "done": chunk_count,
            "total": page_count or None,
            "unit": "chunks" if chunk_count else "pages",
            "updated_at": updated_at,
        }
    return {
        "phase": phase,
        "done": 0,
        "total": chunk_count or None,
        "unit": "chunks",
        "updated_at": updated_at,
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(updated_at: str) -> datetime:
    text = str(updated_at).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _is_stale(updated_at: Optional[str], now: Optional[datetime] = None) -> bool:
    if not updated_at:
        return False
    try:
        parsed = _parse_timestamp(str(updated_at))
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    return (_as_utc(current) - _as_utc(parsed)).total_seconds() > STALE_AFTER_SECONDS


def assemble_progress(
    *,
    stage: Optional[str],
    sqlite: Optional[dict],
    heartbeat: Optional[dict],
) -> Optional[dict]:
    """Combine SQLite snapshot and heartbeat into the runtime ``progress`` object."""
    if stage not in LIVE_PROGRESS_STAGES:
        return None
    phase = STAGE_TO_PHASE.get(stage)
    if not phase:
        return None
    hb = heartbeat if heartbeat and heartbeat.get("phase") == phase else None
    sqlite_done = sqlite.get("done") if sqlite else None
    sqlite_total = sqlite.get("total") if sqlite else None
    hb_done = hb.get("done") if hb else None
    hb_total = hb.get("total") if hb else None
    # Heartbeat totals can be remaining-work (translation retries). Do not mix
    # those with SQLite historical counts against a smaller denominator.
    if hb is not None and hb_total is not None:
        done = hb_done if hb_done is not None else 0
        total = hb_total
    else:
        done_values = [n for n in (sqlite_done, hb_done) if n is not None]
        done = max(done_values) if done_values else 0
        total = hb_total if hb_total is not None else sqlite_total
    sources = []
    if sqlite is not None:
        sources.append("sqlite")
    if hb is not None:
        sources.append("heartbeat")
    if len(sources) == 2:
        source = "mixed"
    elif sources:
        source = sources[0]
    else:
        source = "sqlite"
    updated_at = (hb or {}).get("updated_at") or (sqlite or {}).get("updated_at")
    unit = (hb or sqlite or {}).get("unit") or PHASE_UNIT.get(phase) or "pages"
    return {
        "phase": phase,
        "done": done,
        "total": total,
        "unit": unit,
        "last_updated_at": updated_at,
        "source": source,
        "stale": _is_stale(updated_at),
    }


def _finite_sequence(value: Any) -> list:
    if value is None or isinstance(value, (str, bytes, dict)):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        length = len(value)
    except TypeError:
        return []
    if length <= 0:
        return []
    try:
        return list(value)
    except TypeError:
        return []


async def _heartbeat_dict_from_details(details: Any, converter: Any) -> Optional[dict]:
    if details is None:
        return None
    if isinstance(details, dict):
        return details
    if isinstance(details, (list, tuple)):
        first = details[0] if details else None
        return first if isinstance(first, dict) else None
    payloads = getattr(details, "payloads", None)
    if payloads is None or converter is None:
        return None
    try:
        decoded = await converter.decode_wrapper(details)
    except Exception:
        try:
            decoded = await converter.decode(list(payloads))
        except Exception:
            logging.debug("Could not decode activity heartbeat details", exc_info=True)
            return None
    if decoded and isinstance(decoded[0], dict):
        return decoded[0]
    return None


async def extract_pending_heartbeat(description: Any) -> Optional[dict]:
    """Read the latest pending-activity heartbeat from a Temporal describe() result."""
    if description is None:
        return None
    activities = _finite_sequence(getattr(description, "pending_activities", None))
    if not activities:
        raw = getattr(description, "raw_description", None)
        if raw is not None:
            activities = _finite_sequence(getattr(raw, "pending_activities", None))
    if not activities:
        return None
    converter = getattr(description, "_context_free_data_converter", None)
    if converter is None:
        try:
            from temporalio.converter import DataConverter

            converter = DataConverter.default
        except Exception:
            converter = None
    for info in reversed(activities):
        details = getattr(info, "heartbeat_details", None)
        if details is None:
            details = getattr(info, "raw_heartbeat_details", None)
        payload = await _heartbeat_dict_from_details(details, converter)
        normalized = normalize_heartbeat(payload)
        if normalized:
            if not normalized.get("updated_at"):
                last_hb = getattr(info, "last_heartbeat_time", None)
                if last_hb is not None:
                    iso = getattr(last_hb, "isoformat", None)
                    normalized["updated_at"] = iso() if callable(iso) else str(last_hb)
            return normalized
    return None


async def describe_workflow_cached(
    client: Any, temporal_workflow_id: str, handle: Any = None
) -> Any:
    """Return handle.describe(), coalescing duplicate calls for ~2s per workflow id."""
    now = time.monotonic()
    _prune_describe_cache(now)
    cached = _describe_cache.get(temporal_workflow_id)
    if cached and (now - cached[0]) < DESCRIBE_TTL_SECONDS:
        return cached[1]
    handle = handle or client.get_workflow_handle(temporal_workflow_id)
    description = await handle.describe()
    _describe_cache[temporal_workflow_id] = (now, description)
    return description


async def progress_for_runtime(
    *,
    workflow_id: str,
    doc: dict,
    chunking_progress: Optional[dict],
    description: Any,
    temporal_connected: bool,
    describe_ok: bool,
) -> Optional[dict]:
    if not live_progress_enabled():
        return None
    if not temporal_connected or not describe_ok:
        return None
    if doc.get("stage") not in LIVE_PROGRESS_STAGES:
        return None
    sqlite = sqlite_progress_snapshot(
        workflow_id, doc, chunking_progress=chunking_progress
    )
    heartbeat = await extract_pending_heartbeat(description)
    return assemble_progress(
        stage=doc.get("stage"), sqlite=sqlite, heartbeat=heartbeat
    )
