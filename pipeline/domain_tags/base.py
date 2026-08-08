"""Domain tag types and taxonomy helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

TAXONOMY_PATH = Path(__file__).resolve().parent / "taxonomy.json"


def _default_taxonomy_path() -> Path:
    override = (os.environ.get("DOMAIN_TAXONOMY_PATH") or "").strip()
    if override:
        return Path(override)
    return TAXONOMY_PATH


@dataclass(frozen=True)
class DomainTag:
    dimension: str
    value: str
    source: str = "auto"  # auto | manual

    def key(self) -> str:
        return f"{self.dimension}:{self.value}"

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "source": self.source,
            "tag": self.key(),
        }


def load_taxonomy(path: Path | None = None) -> dict:
    taxonomy_file = path or _default_taxonomy_path()
    with open(taxonomy_file, encoding="utf-8") as handle:
        return json.load(handle)


def load_taxonomy_for_instance(instance: str | None) -> dict:
    """Resolve the taxonomy for a specific tenant.

    Returns the tenant's own DB-backed taxonomy when it has been seeded/edited,
    else falls back to the shipped file default (which is also what every
    tenant's taxonomy is seeded from — so the default tenant, and any tenant not
    yet seeded, get identical behaviour to the legacy static file). The ``db``
    import is lazy so the tagging package keeps no static dependency on it.

    The fallback is gated on the seed marker, not on emptiness: a seeded tenant
    answers with its own rows even when there are none, so tags an admin deleted
    are never resurrected here and re-applied to documents. Mirrors
    ``api._tenant_taxonomy_payload``.
    """
    inst = (instance or "").strip().lower()
    if inst:
        try:
            from .. import db
            tenant_taxonomy = db.get_taxonomy(inst)
            seeded = db.taxonomy_is_seeded(inst)
        except Exception:  # noqa: BLE001 - never let a read fall over on tagging
            tenant_taxonomy, seeded = None, False
        if tenant_taxonomy:
            return tenant_taxonomy
        if seeded:
            return {"instance": inst, "domains": {}}
    return load_taxonomy()


def flatten_taxonomy_values(taxonomy: dict | None = None) -> dict[str, set[str]]:
    taxonomy = taxonomy or load_taxonomy()
    allowed: dict[str, set[str]] = {}
    for domain in taxonomy.get("domains", {}).values():
        if not isinstance(domain, dict):
            continue
        for dimension, values in domain.items():
            if not isinstance(values, list):
                continue
            # Lowercase so values match normalize_tag_key() (FMD -> fmd, etc.)
            allowed.setdefault(dimension, set()).update(
                v.strip().lower() for v in values if isinstance(v, str) and v.strip()
            )
    return allowed


def normalize_tag_key(raw: str) -> str | None:
    text = (raw or "").strip().lower()
    if not text or ":" not in text:
        return None
    dimension, value = text.split(":", 1)
    dimension = dimension.strip()
    value = value.strip()
    if not dimension or not value:
        return None
    return f"{dimension}:{value}"


def parse_tag_list(tags: Iterable[str], *, source: str = "manual") -> list[DomainTag]:
    parsed: list[DomainTag] = []
    seen: set[str] = set()
    for raw in tags:
        key = normalize_tag_key(raw if isinstance(raw, str) else str(raw))
        if not key or key in seen:
            continue
        dimension, value = key.split(":", 1)
        parsed.append(DomainTag(dimension=dimension, value=value, source=source))
        seen.add(key)
    return parsed


def validate_tags_against_taxonomy(
    tags: list[DomainTag],
    taxonomy: dict | None = None,
    *,
    strict: bool = False,
) -> list[DomainTag]:
    """Return tags, optionally dropping unknown dimension:value pairs."""
    allowed = flatten_taxonomy_values(taxonomy)
    if not strict:
        return tags
    validated: list[DomainTag] = []
    for tag in tags:
        values = allowed.get(tag.dimension)
        # Unknown dimensions and empty vocab lists are rejected in strict mode.
        if values is not None and values and tag.value in values:
            validated.append(tag)
    return validated


# How a tag set is encoded into — and filtered out of — Marqo's flat
# ``domain_tags`` field is Marqo's grammar, not the taxonomy's: it lives in
# ``pipeline.vector_store`` alongside every other ``field:value`` string we build.
# ``split_query_and_tags`` below is pure text with a SQLite-only caller, so it
# stays here.


def split_query_and_tags(query: str) -> tuple[str, list[str]]:
    """Extract dimension:value tokens from a free-text query for chunk search."""
    import re

    text = (query or "").strip()
    if not text:
        return "", []

    tag_pattern = re.compile(
        r"(?:^|\s)([a-z][a-z0-9_-]*:[a-z0-9][\w/.-]*)",
        re.IGNORECASE,
    )
    tags: list[str] = []
    seen: set[str] = set()
    for match in tag_pattern.finditer(text):
        key = normalize_tag_key(match.group(1))
        if key and key not in seen:
            tags.append(key)
            seen.add(key)

    remaining = tag_pattern.sub(" ", text)
    remaining = " ".join(remaining.split())
    return remaining, tags
