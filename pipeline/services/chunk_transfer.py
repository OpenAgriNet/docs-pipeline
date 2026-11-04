"""Portable chunk serialization, independent of HTTP and vector-store payloads."""

import csv
import io
import json
import unicodedata
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone


SCHEMA_VERSION = 2
CSV_ENCODING = "backslash-v1"
EXPORT_FIELDS = (
    "schema_version", "chunk_number", "text", "original_text", "edited_text",
    "token_count", "section_title", "content_type", "is_reference", "is_excluded",
    "page_start", "page_end", "source_page_numbers_json", "source_spans_json",
    "domain_tags", "domain_tag_metadata", "is_reviewed", "reviewer_notes",
    "chunk_version", "chunking_provider", "chunking_model", "chunking_config_json",
    "chunking_run_id", "source_workflow_id", "source_document_id",
    "source_filename", "source_instance", "source_display_name", "source_stage",
    "source_query_enabled", "source_is_disabled", "exported_at",
)
CSV_FIELDS = (*EXPORT_FIELDS, "csv_encoding")
_JSON_CSV_FIELDS = {
    "schema_version", "chunk_number", "token_count", "page_start", "page_end",
    "is_reference", "is_excluded", "is_reviewed", "chunk_version",
    "domain_tags", "domain_tag_metadata", "source_query_enabled", "source_is_disabled",
}
_ESCAPE_PREFIXES = frozenset("=+-@\\'\"＝＋－＠")


def portable_chunk(chunk: dict, *, exported_at: str) -> dict:
    """Keep exact text/history; `text` is the authoritative editable transfer field.

    Source identity, review history and document state are informational, never
    authority for a future import's ownership, approval or search enablement.
    """
    record = {name: chunk.get(name) for name in EXPORT_FIELDS}
    tags = chunk.get("domain_tags") or []
    record.update(
        schema_version=SCHEMA_VERSION,
        exported_at=exported_at,
        text=chunk.get("edited_text") or chunk.get("original_text") or "",
        domain_tags=sorted({f"{tag['dimension']}:{tag['value']}" for tag in tags}),
        domain_tag_metadata=tags,
        source_workflow_id=chunk["workflow_id"],
        source_document_id=chunk.get("document_id"),
        source_filename=chunk.get("filename"),
        source_instance=chunk.get("instance"),
    )
    return record


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    # A visible backslash is literal spreadsheet data, rather than a quote/tab
    # escape that a spreadsheet may strip. Escape literal leading backslashes
    # too, so decoding never confuses them with an added prefix. A lone backslash
    # represents an empty string, preserving its distinction from null.
    if (
        not value or value[0] in _ESCAPE_PREFIXES or value[0].isspace()
        or unicodedata.category(value[0]) in {"Cc", "Cf"}
    ):
        return "\\" + value
    return value


def decode_csv_record(row: dict[str, str]) -> dict:
    """Reverse this export's CSV encoding; no import/persistence side effects.

    Future import validation must separately enforce schema, tenant ownership,
    new-document identity and the authoritative `text`/`domain_tags` fields.
    """
    if set(row) != set(CSV_FIELDS) or row.get("csv_encoding") != CSV_ENCODING:
        raise ValueError("Unsupported chunk CSV columns or encoding")
    record = {}
    for name in EXPORT_FIELDS:
        cell = row[name]
        if not isinstance(cell, str):
            raise ValueError(f"Missing CSV cell: {name}")
        if cell == "":
            record[name] = None
        elif name in _JSON_CSV_FIELDS:
            try:
                record[name] = json.loads(cell)
            except ValueError as exc:
                raise ValueError(f"Invalid JSON value in CSV column: {name}") from exc
        else:
            record[name] = cell[1:] if cell.startswith("\\") else cell
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported chunk schema version")
    return record


def serialize_chunks(chunks: Iterable[dict], file_format: str) -> Iterator[str]:
    """Stream canonical JSON or reversibly escaped UTF-8 CSV from saved data."""
    if file_format not in {"json", "csv"}:
        raise ValueError("Chunk format must be json or csv")
    # Explicitly close the database generator when streaming is interrupted.
    iterator = iter(chunks)
    # One timestamp for the entire download, including streamed multi-doc rows.
    exported_at = datetime.now(timezone.utc).isoformat()
    try:
        if file_format == "json":
            yield "["
            separator = ""
            for chunk in iterator:
                yield separator + json.dumps(
                    portable_chunk(chunk, exported_at=exported_at),
                    ensure_ascii=False, allow_nan=False,
                )
                separator = ",\n"
            yield "]\n"
        else:
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            # BOM allows common spreadsheet apps to recognize Gujarati UTF-8.
            yield "\ufeff" + buffer.getvalue()
            for chunk in iterator:
                buffer.seek(0)
                buffer.truncate(0)
                record = {
                    name: _csv_cell(value)
                    for name, value in portable_chunk(chunk, exported_at=exported_at).items()
                }
                record["csv_encoding"] = CSV_ENCODING
                writer.writerow(record)
                yield buffer.getvalue()
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
