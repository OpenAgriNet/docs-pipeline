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


def flatten_taxonomy_values(taxonomy: dict | None = None) -> dict[str, set[str]]:
    taxonomy = taxonomy or load_taxonomy()
    allowed: dict[str, set[str]] = {}
    for domain in taxonomy.get("domains", {}).values():
        if not isinstance(domain, dict):
            continue
        for dimension, values in domain.items():
            if not isinstance(values, list):
                continue
            allowed.setdefault(dimension, set()).update(v.strip() for v in values if v)
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
        if values and tag.value in values:
            validated.append(tag)
    return validated


def tags_to_marqo_field(tags: list[DomainTag]) -> str:
    """Pipe-separated flat tag string for Marqo filter field."""
    keys = sorted({tag.key() for tag in tags})
    return "|".join(keys)


def tags_from_marqo_field(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def build_marqo_domain_tags_filter(tags: Iterable[str]) -> str | None:
    """Build a Marqo filter clause requiring all listed dimension:value tags."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        key = normalize_tag_key(raw if isinstance(raw, str) else str(raw))
        if not key or key in seen:
            continue
        normalized.append(key)
        seen.add(key)
    if not normalized:
        return None
    clauses = [f"domain_tags:({tag})" for tag in normalized]
    return " AND ".join(clauses)


def merge_marqo_filter_strings(*parts: str | None) -> str | None:
    clauses = [part.strip() for part in parts if part and part.strip()]
    if not clauses:
        return None
    return " AND ".join(clauses)


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
