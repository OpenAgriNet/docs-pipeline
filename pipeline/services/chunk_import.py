"""Create managed records from prepared chunks; never start jobs or index data."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .. import db
from ..auth.models import AuthUser
from ..chunking.base import count_tokens
from ..domain_tags.base import (
    load_taxonomy_for_instance, normalize_tag_key, parse_tag_list,
    validate_tags_against_taxonomy,
)
from ..domain_tags.service import load_domain_tagging_config
from ..storage import minio as storage
from .access import resolve_create_instance
from .chunk_transfer import EXPORT_FIELDS, SCHEMA_VERSION, decode_csv_record


class ChunkImportError(ValueError):
    """Invalid transfer input; validation has made no persistent writes."""


class ImportCleanupError(RuntimeError):
    """Persistence failed and one or more new objects could not be removed."""

    def __init__(self, uris: list[str]):
        self.orphaned_uris = uris
        super().__init__(f"Import failed; artifact cleanup required: {', '.join(uris)}")


@dataclass(frozen=True)
class ImportLimits:
    """Server-owned limits, never accepted from the uploaded file."""

    max_upload_bytes: int = 25 * 1024 * 1024
    max_rows: int = 10_000
    max_text_bytes: int = 256 * 1024
    max_tags_per_chunk: int = 64
    max_pdf_bytes: int = 100 * 1024 * 1024
    max_pdf_pages: int = 10_000


_INTEGER_FIELDS = {"schema_version", "chunk_number", "token_count", "page_start", "page_end", "chunk_version"}
_BOOLEAN_FIELDS = {"is_reference", "is_excluded", "is_reviewed", "source_query_enabled", "source_is_disabled"}
_JSON_FIELDS = {"domain_tags", "domain_tag_metadata", "source_page_numbers_json", "source_spans_json", "chunking_config_json"}
_SOURCE_IDS = ("source_workflow_id", "source_document_id", "source_instance")


def _json(text: str):
    def invalid_constant(value):
        raise ChunkImportError(f"Invalid JSON constant: {value}")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ChunkImportError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ChunkImportError("JSON number is outside the finite range")
        return number

    return json.loads(text, parse_constant=invalid_constant, parse_float=finite_float,
                      object_pairs_hook=unique_keys)


def _parse(content: bytes, file_format: str, limits: ImportLimits) -> list[dict]:
    if not isinstance(content, bytes) or not content or len(content) > limits.max_upload_bytes:
        raise ChunkImportError("Chunk upload is empty or exceeds the size limit")
    try:
        text = content.decode("utf-8-sig")
        if file_format == "json":
            rows = _json(text)
            if not isinstance(rows, list):
                raise ChunkImportError("JSON must be an array of chunk objects")
        elif file_format == "csv":
            # csv's default 128 KiB field ceiling is below our allowed text
            # size. Only raise its process-wide ceiling; upload/row limits here
            # still bound each parser independently, including concurrent calls.
            csv.field_size_limit(max(csv.field_size_limit(), limits.max_upload_bytes))
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not {"chunk_number", "text"} <= set(fields):
                raise ChunkImportError("CSV needs unique columns including chunk_number and text")
            if set(fields) - set(EXPORT_FIELDS) - {"csv_encoding"}:
                raise ChunkImportError("Unknown CSV columns")
            rows = []
            for row in reader:
                if len(rows) >= limits.max_rows:
                    raise ChunkImportError("Too many chunks")
                if None in row or any(value is None for value in row.values()):
                    raise ChunkImportError("CSV row does not match its header")
                if "csv_encoding" in fields:
                    # Apply the same strict JSON rules to typed CSV cells as to
                    # JSON uploads, before the portable decoder loses duplicate
                    # keys or accepts non-standard floating-point constants.
                    for name in (_INTEGER_FIELDS | _BOOLEAN_FIELDS | {"domain_tags", "domain_tag_metadata"}) & row.keys():
                        if row[name] != "":
                            _json(row[name])
                    rows.append(decode_csv_record(row))
                    continue
                # Plain CSV has no escaping. Do not strip literal backslashes.
                parsed = dict(row)
                for name in set(row) & (_INTEGER_FIELDS | _BOOLEAN_FIELDS | _JSON_FIELDS):
                    value = _json(row[name]) if row[name] != "" else None
                    if name in _BOOLEAN_FIELDS and type(value) is int and value in (0, 1):
                        value = bool(value)
                    parsed[name] = value
                rows.append(parsed)
        else:
            raise ChunkImportError("Chunk format must be json or csv")
    except (ValueError, UnicodeError, csv.Error, RecursionError) as exc:
        raise ChunkImportError(f"Invalid {file_format} chunk file: {exc}") from exc
    if not rows or len(rows) > limits.max_rows:
        raise ChunkImportError("Chunk count is empty or exceeds the row limit")
    return rows


def _integer(value, name: str, *, minimum=1, maximum=2_147_483_647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ChunkImportError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _metadata(value, name: str, kind):
    if value is None:
        return None
    try:
        decoded = _json(value) if isinstance(value, str) else value
    except (ValueError, RecursionError) as exc:
        raise ChunkImportError(f"Invalid {name}") from exc
    if not isinstance(decoded, kind):
        raise ChunkImportError(f"{name} has the wrong JSON type")
    return decoded


def _pdf_pages(content: bytes | None, limits: ImportLimits) -> int:
    if content is None:
        return 0
    if not isinstance(content, bytes) or not content or len(content) > limits.max_pdf_bytes or not content.startswith(b"%PDF-"):
        raise ChunkImportError("Source PDF is empty, invalid or exceeds the size limit")
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        pages = len(reader.pages)
        if not 1 <= pages <= limits.max_pdf_pages:
            raise ValueError("PDF page count exceeds the allowed range")
        return pages
    except Exception as exc:
        raise ChunkImportError(f"Invalid source PDF: {exc}") from exc


def _validate(rows: list[dict], instance: str, pages: int, limits: ImportLimits) -> list[dict]:
    chunks, numbers = [], set()
    identities = {key: set() for key in _SOURCE_IDS}
    strict_tags = load_domain_tagging_config().strict_taxonomy
    taxonomy = load_taxonomy_for_instance(instance) if strict_tags else None
    for position, row in enumerate(rows, 1):
        try:
            if not isinstance(row, dict) or set(row) - set(EXPORT_FIELDS):
                raise ChunkImportError("Expected a chunk object with supported fields")
            # Marked CSV's decoder uses Python JSON semantics; explicitly reject
            # NaN/Infinity even when nested inside provenance-only metadata.
            json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8")
            for name in _INTEGER_FIELDS & row.keys():
                if row[name] is not None:
                    _integer(row[name], name, minimum=0 if name == "token_count" else 1)
            if row.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
                raise ChunkImportError("Unsupported chunk schema version")
            for name in _BOOLEAN_FIELDS & row.keys():
                if row[name] is not None and type(row[name]) is not bool:
                    raise ChunkImportError(f"{name} must be a boolean")
            for name in set(row) - _INTEGER_FIELDS - _BOOLEAN_FIELDS - _JSON_FIELDS:
                if row[name] is not None and not isinstance(row[name], str):
                    raise ChunkImportError(f"{name} must be a string")
            # Existing chunk read/edit/delete routes accept IDs up to 10,000.
            # A row-count limit alone does not constrain gapped numbering.
            number = _integer(row.get("chunk_number"), "chunk_number", maximum=10_000)
            if number in numbers:
                raise ChunkImportError("Duplicate chunk_number")
            numbers.add(number)
            text = row.get("text")
            if not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise ChunkImportError("text must be non-empty text without NUL characters")
            if len(text.encode("utf-8")) > limits.max_text_bytes:
                raise ChunkImportError("Chunk text exceeds the size limit")
            for key in identities:
                if row.get(key):
                    identities[key].add(row[key])
                    if len(identities[key]) > 1:
                        raise ChunkImportError("File contains more than one source document")
            start, end = row.get("page_start"), row.get("page_end")
            if pages and (start is None or end is None):
                raise ChunkImportError("Chunks with a source PDF need page_start and page_end")
            if (start is None) != (end is None) or (start is not None and start > end):
                raise ChunkImportError("page_start and page_end must form a complete ordered range")

            def check_page(page):
                _integer(page, "source page")
                if pages and page > pages:
                    raise ChunkImportError("Page is outside the source PDF")
                if start is not None and not start <= page <= end:
                    raise ChunkImportError("Source page is outside the chunk page range")

            if start is not None and pages and end > pages:
                raise ChunkImportError("Page range is outside the source PDF")
            page_numbers = _metadata(row.get("source_page_numbers_json"), "source_page_numbers_json", list)
            spans = _metadata(row.get("source_spans_json"), "source_spans_json", list)
            for page in page_numbers or []:
                check_page(page)
            for span in spans or []:
                if not isinstance(span, dict) or not ({"page_number", "page"} & span.keys()):
                    raise ChunkImportError("Source span needs page_number")
                check_page(span.get("page_number", span.get("page")))
                if "page_number" in span and "page" in span and span["page_number"] != span["page"]:
                    raise ChunkImportError("Source span has conflicting page numbers")
                left, right = span.get("start_char"), span.get("end_char")
                if left is not None or right is not None:
                    _integer(left, "start_char", minimum=0)
                    _integer(right, "end_char", minimum=0)
                    if left > right:
                        raise ChunkImportError("Source span character bounds are reversed")
            raw_tags = row.get("domain_tags")
            if raw_tags is None:
                raw_tags = []
            if not isinstance(raw_tags, list) or len(raw_tags) > limits.max_tags_per_chunk:
                raise ChunkImportError("domain_tags must be a list within the tag limit")
            if any(not isinstance(tag, str) or not normalize_tag_key(tag) for tag in raw_tags):
                raise ChunkImportError("Each domain tag must be dimension:value")
            tags = parse_tag_list(raw_tags, source="manual")
            if strict_tags:
                allowed = validate_tags_against_taxonomy(tags, taxonomy, strict=True)
                if len(allowed) != len(tags):
                    raise ChunkImportError("Domain tag is not allowed by the tenant taxonomy")
            # History stays in the immutable source artifact, never as authority
            # for active text/tags, approvals or the new document's generation.
            _metadata(row.get("domain_tag_metadata"), "domain_tag_metadata", list)
            _metadata(row.get("chunking_config_json"), "chunking_config_json", dict)
            chunks.append({
                "chunk_number": number, "text": text,
                "token_count": row["token_count"] if row.get("token_count") is not None else count_tokens(text),
                "page_start": start, "page_end": end,
                "source_page_numbers_json": json.dumps(page_numbers) if page_numbers is not None else None,
                "source_spans_json": json.dumps(spans) if spans is not None else None,
                "section_title": row.get("section_title"), "content_type": row.get("content_type"),
                "is_reference": bool(row.get("is_reference")),
                "is_excluded": bool(row.get("is_excluded")), "tags": tags,
            })
        except (ValueError, RecursionError) as exc:
            raise ChunkImportError(f"Chunk row {position}: {exc}") from exc
    return chunks


def _filename(value: str, suffix: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 255
            or "/" in value or "\\" in value or any(ord(c) < 32 for c in value)
            or Path(value).suffix.lower() != suffix):
        raise ChunkImportError(f"Expected a filename ending in {suffix}, without a path")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ChunkImportError("Filename must be valid UTF-8 text") from exc
    return value


def create_prepared_import(
    *, user: AuthUser, instance: str | None, document_id: str,
    filename: str, content: bytes, file_format: str,
    pdf_content: bytes | None = None, pdf_filename: str | None = None,
    limits: ImportLimits = ImportLimits(),
) -> dict:
    """Validate everything, upload unique artifacts, then create SQL atomically.

    The input artifact preserves source IDs/history exactly. Caller-supplied IDs
    never become workflow/object identity. All persisted chunks remain unreviewed
    and the document stays unindexed with query_enabled=False. A later service
    must explicitly publish/verify a configured DEV destination.
    """
    tenant = resolve_create_instance(user, instance)
    if (not isinstance(document_id, str) or not document_id.strip() or len(document_id) > 255
            or document_id != document_id.strip() or any(ord(c) < 32 for c in document_id)):
        raise ChunkImportError("document_id must be non-empty text of at most 255 characters")
    try:
        document_id.encode("utf-8")
    except UnicodeError as exc:
        raise ChunkImportError("document_id must be valid UTF-8 text") from exc
    if file_format not in {"json", "csv"}:
        raise ChunkImportError("Chunk format must be json or csv")
    _filename(filename, f".{file_format}")
    if pdf_content is not None:
        _filename(pdf_filename, ".pdf")
    elif pdf_filename is not None:
        raise ChunkImportError("PDF filename was supplied without a PDF")
    rows = _parse(content, file_format, limits)
    pages = _pdf_pages(pdf_content, limits)
    chunks = _validate(rows, tenant, pages, limits)
    fingerprint = hashlib.sha256(content).hexdigest()
    with db.get_connection() as conn:
        db.assert_prepared_import_available(conn, tenant, document_id, fingerprint)

    workflow_id = f"prepared-{uuid.uuid4().hex}"
    artifacts, attempted_objects = [], []
    uploads = [(content, filename, "original_upload")]
    if pdf_content is not None:
        uploads.append((pdf_content, pdf_filename, "normalized_pdf"))
    try:
        with tempfile.TemporaryDirectory(prefix="prepared-chunks-") as directory:
            for index, (data, name, kind) in enumerate(uploads):
                path = Path(directory) / f"{index}{Path(name).suffix.lower()}"
                path.write_bytes(data)
                # Remember the generated key BEFORE fput: an upload can succeed
                # remotely and then time out before returning its URI locally.
                bucket = storage.bucket_name()
                key = storage.artifact_object_name(tenant, workflow_id, kind, name)
                attempted_objects.append((bucket, key))
                uri, size, mime = storage.upload_artifact(
                    str(path), workflow_id, kind, name, instance=tenant,
                )
                artifacts.append({
                    "artifact_type": kind, "storage_uri": uri, "size_bytes": size,
                    "mime_type": mime, "filename": name,
                    "metadata": {"import_format": file_format, "imported_by": user.user_id,
                                 "sha256": hashlib.sha256(data).hexdigest()},
                })
        document = {
            "workflow_id": workflow_id, "document_id": document_id, "filename": pdf_filename or filename,
            "instance": tenant, "page_count": pages, "source_file_fingerprint": fingerprint,
        }
        db.create_prepared_document(document, chunks, artifacts)
    except Exception as exc:
        orphaned = []
        for bucket, key in reversed(attempted_objects):
            try:
                storage.delete_object(bucket, key)
            except Exception:
                orphaned.append(f"minio://{bucket}/{key}")
        if orphaned:
            raise ImportCleanupError(orphaned) from exc
        raise
    # No post-commit I/O that could be mistaken for a failed/rolled-back import.
    return {**document, "chunk_count": len(chunks), "query_enabled": False,
            "stage": "ready_for_ingestion"}
