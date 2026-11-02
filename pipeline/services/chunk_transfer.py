"""Portable chunk serialization, independent of HTTP and vector-store payloads."""

import csv
import io
import json
from collections.abc import Iterable, Iterator


EXPORT_FIELDS = (
    "schema_version", "chunk_number", "text", "original_text", "edited_text",
    "token_count", "section_title", "content_type", "is_reference", "is_excluded",
    "page_start", "page_end", "source_page_numbers_json", "source_spans_json",
    "domain_tags", "domain_tag_metadata", "is_reviewed", "reviewer_notes",
    "chunk_version", "chunking_provider", "chunking_model", "chunking_config_json",
    "chunking_run_id", "source_workflow_id", "source_document_id",
    "source_filename", "source_instance",
)


def portable_chunk(chunk: dict) -> dict:
    """Keep source identity as provenance, separate from a future import's IDs."""
    record = {name: chunk.get(name) for name in EXPORT_FIELDS}
    tags = chunk.get("domain_tags") or []
    record.update(
        schema_version=1,
        text=chunk.get("edited_text") or chunk.get("original_text") or "",
        domain_tags=sorted({f"{tag['dimension']}:{tag['value']}" for tag in tags}),
        domain_tag_metadata=tags,
        source_workflow_id=chunk["workflow_id"],
        source_document_id=chunk.get("document_id"),
        source_filename=chunk.get("filename"),
        source_instance=chunk.get("instance"),
    )
    return record


def serialize_chunks(chunks: Iterable[dict], file_format: str) -> Iterator[str]:
    """Stream a JSON array or UTF-8 CSV; never rewrite text to escape formulas.

    CSV is a data-interchange format: text must be imported as text in spreadsheet
    apps. JSON is the lossless choice for nulls versus empty strings.
    """
    if file_format not in {"json", "csv"}:
        raise ValueError("Chunk format must be json or csv")
    # Explicitly close the database generator when streaming is interrupted.
    iterator = iter(chunks)
    try:
        if file_format == "json":
            yield "["
            separator = ""
            for chunk in iterator:
                yield separator + json.dumps(portable_chunk(chunk), ensure_ascii=False)
                separator = ",\n"
            yield "]\n"
        else:
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            # BOM allows common spreadsheet apps to recognize Gujarati UTF-8.
            yield "\ufeff" + buffer.getvalue()
            for chunk in iterator:
                buffer.seek(0)
                buffer.truncate(0)
                record = portable_chunk(chunk)
                for name, value in record.items():
                    if isinstance(value, (list, dict, bool)):
                        record[name] = json.dumps(value, ensure_ascii=False)
                writer.writerow(record)
                yield buffer.getvalue()
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
