"""Pure construction of vector-store records and provenance metadata."""

import csv
import hashlib
import json
import logging
import os
import re
from threading import Lock


_doc_metadata_cache: dict[str, dict] = {}
_doc_descriptions_cache: dict[str, str] = {}
_metadata_loaded = False
_metadata_lock = Lock()


def _normalize_filename(name: str) -> str:
    return (name or "").strip().lower()


def _load_metadata_once() -> None:
    global _metadata_loaded
    if _metadata_loaded:
        return
    with _metadata_lock:
        if _metadata_loaded:
            return

        metadata_csv_path = os.getenv(
            "DOCUMENT_METADATA_CSV_PATH", "/app/workspace/document_manifest.csv"
        )
        descriptions_jsonl_path = os.getenv(
            "DOCUMENT_DESCRIPTIONS_JSONL_PATH",
            "/app/workspace/document_descriptions.jsonl",
        )

        if os.path.exists(metadata_csv_path):
            try:
                with open(metadata_csv_path, "r", encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        file_name = (row.get("File Name") or "").strip()
                        if not file_name or file_name.startswith(("http://", "https://")):
                            continue
                        _doc_metadata_cache[_normalize_filename(file_name)] = {
                            "title_en": (row.get("Title (English)") or "").strip(),
                            "title_gu": (row.get("Title (Gujarati)") or "").strip(),
                            "doc_language": (
                                row.get("Language (Gujarati / English)") or ""
                            ).strip(),
                            "category_tags": (row.get("Category Tags (") or "").strip(),
                            "doc_short_description": (row.get("Description") or "").strip(),
                            "quality_score": (row.get("Quality(1-5)") or "").strip(),
                            "priority_rank": (row.get("Priority(1-5)") or "").strip(),
                            "ingestion_status": (
                                row.get("Status ingested in the system") or ""
                            ).strip(),
                        }
            except Exception as exc:  # noqa: BLE001 - optional enrichment file
                logging.warning("Failed loading document metadata CSV: %s", exc)

        if os.path.exists(descriptions_jsonl_path):
            try:
                with open(descriptions_jsonl_path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        value = json.loads(line)
                        key = _normalize_filename(value.get("file", ""))
                        if key and value.get("description"):
                            _doc_descriptions_cache[key] = str(value["description"]).strip()
            except Exception as exc:  # noqa: BLE001 - optional enrichment file
                logging.warning("Failed loading document descriptions JSONL: %s", exc)

        _metadata_loaded = True


def _get_doc_metadata(filename: str) -> dict:
    _load_metadata_once()
    return _doc_metadata_cache.get(_normalize_filename(filename), {})


def _get_doc_description(filename: str) -> str:
    _load_metadata_once()
    return _doc_descriptions_cache.get(_normalize_filename(filename), "")


def clean_html_tags(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    return re.sub(r"<[^>]+>", "", text).strip()


def clean_latex_notation(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = re.sub(r"\[\^[0-9]+\]", "", text)
    text = re.sub(r"\$\s*\{\s*\}\s*\^\{[0-9]+\}\s*\$", "", text)
    text = re.sub(r"\$\s*\^\{[0-9]+\}\s*\$", "", text)
    text = re.sub(r"\$\s*\$", "", text)
    text = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    text = re.sub(r"[\\{}]", "", text)
    return text


def format_table_content(text: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned_lines = []
    for line in text.split("\n"):
        if re.match(r"^[\s\|]*$", line) or re.match(r"^[\s\|\-\:]*$", line):
            continue
        line = re.sub(r"\|\s*\|", "|", line)
        line = re.sub(r"^\|\s*", "", line)
        line = re.sub(r"\s*\|$", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def clean_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = clean_html_tags(text)
    text = clean_latex_notation(text)
    text = format_table_content(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n\s*\n\s*\n", "\n\n", text)
    text = re.sub(r"\n ", "\n", text)
    text = re.sub(r" \n", "\n", text)
    return text.strip()


def infer_section(text: str, section_title: str | None = None) -> str:
    """Return the explicit or first Markdown section heading."""
    if section_title and str(section_title).strip():
        return str(section_title).strip()
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def is_reference_section(text: str) -> bool:
    """Return whether text is primarily citations or bibliography content."""
    if not text or len(text) < 50:
        return False
    ref_headers = [
        r"^\s*#{1,3}\s*(?:references|bibliography|citations|works cited|literature cited)\s*$",
        r"^\s*\*{1,2}(?:references|bibliography)\*{1,2}\s*$",
    ]
    if any(re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in ref_headers):
        return True

    lines = text.split("\n")
    total_lines = len([line for line in lines if line.strip()])
    if total_lines == 0:
        return False
    citation_patterns = [
        r"^\s*\d{1,3}[\.\)]\s+[A-Z][a-z]+[\s,].*(?:\d{4}|\(\d{4}\))",
        r"doi[:\s]*10\.\d{4,}",
        r"(?:J\.|Journal|Int\.|Proceedings|Trans\.).*\d{4}",
        r"\(\d{4}\)\s*$",
        r"\bet\s+al\b",
        r"(?:Vol\.?\s*\d+|\d+\s*\(\d+\)\s*:)",
        r"(?:pp?\.?\s*\d+[-–]\d+|:\s*\d+[-–]\d+)",
    ]
    citation_lines = sum(
        1
        for line in lines
        if line.strip()
        and any(re.search(pattern, line, re.IGNORECASE) for pattern in citation_patterns)
    )
    return (citation_lines / total_lines) > 0.4


def clean_text_for_ingestion(text: str) -> str:
    """Remove translation preambles before vector-store ingestion."""
    if not text:
        return text
    result = text
    result = re.sub(
        r"^Here is the translated text from \*\*[^*]+\*\* to English[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text from [^:]+?:\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the (?:English )?translation[^:]*:?\s*\n*",
        "",
        result,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    result = re.sub(
        r"^Here is the translated text with[^:]+:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    prefixes = [
        r"^(?:the\s+)?english\s+translation:?\s*\n*",
        r"^(?:the\s+)?translation:?\s*\n*",
        r"^translated\s+(?:text|content):?\s*\n*",
        r"^##?\s*(?:english\s+)?translation\s*\n+",
        r"^---+\s*\n+",
        r"^\*\*Translation:?\*\*\s*\n*",
    ]
    for pattern in prefixes:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE | re.MULTILINE)
    result = re.sub(r"\n*-{3,}\s*$", "", result)
    return result.strip()


def _normalize_instance(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text:
        return text
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def get_marqo_record_id(
    chunk_num: int,
    *,
    workflow_id: str | None = None,
    document_id: str | None = None,
) -> str:
    """Stable Marqo ``_id`` for a chunk — independent of chunk text (#122).

    Prefer ``workflow_id`` (one pipeline run). Fall back to ``document_id`` only
    when no workflow is available (sample/admin paths); co-resident documents
    sharing a content hash still need ``workflow_id`` for scoped purge (#73).
    """
    if workflow_id and str(workflow_id).strip():
        return hashlib.md5(f"{workflow_id.strip()}:{chunk_num}".encode()).hexdigest()
    if document_id and str(document_id).strip():
        doc_hash = hashlib.md5(document_id.encode()).hexdigest()
        return hashlib.md5(f"{doc_hash}:{chunk_num}".encode()).hexdigest()
    raise ValueError("workflow_id or document_id required for Marqo record _id")


def prepare_records(
    document_id: str,
    filename: str,
    chunks: list[dict],
    workflow_id: str | None = None,
    name_gu: str | None = None,
    name_en: str | None = None,
    description: str | None = None,
    include_e5_prefix_field: bool = True,
    instance: str | None = None,
) -> list[dict]:
    metadata = _get_doc_metadata(filename)
    resolved_instance = _normalize_instance(instance)
    llm_doc_description = _get_doc_description(filename)
    external_slug = workflow_id or filename
    default_name = filename.replace(".pdf", "").replace(".PDF", "")
    name_gu = name_gu or metadata.get("title_gu") or default_name
    name_en = name_en or metadata.get("title_en") or default_name
    quality_score = metadata.get("quality_score", "")
    priority_rank = metadata.get("priority_rank", "")
    effective_description = (
        description
        or llm_doc_description
        or metadata.get("doc_short_description", "")
    )

    records = []
    for chunk in chunks:
        raw_text = chunk.get("edited_text") or chunk.get("original_text", "")
        chunk_num = chunk.get("chunk_number", 0)
        text = clean_text_for_ingestion(raw_text)
        record = {
            "_id": get_marqo_record_id(
                chunk_num,
                workflow_id=workflow_id,
                document_id=document_id,
            ),
            "doc_id": document_id,
            "workflow_id": workflow_id or "",
            "instance": resolved_instance,
            "type": "document",
            "source": "docs-pipeline",
            "filename": external_slug,
            "name_gu": name_gu,
            "name_en": name_en,
            "title_en": metadata.get("title_en", ""),
            "title_gu": metadata.get("title_gu", ""),
            "doc_language": metadata.get("doc_language", ""),
            "category_tags": metadata.get("category_tags", ""),
            "doc_short_description": metadata.get("doc_short_description", ""),
            "doc_llm_description": llm_doc_description,
            "ingestion_status": metadata.get("ingestion_status", ""),
            "description": effective_description,
            "text": text,
            "chunk_num": chunk_num,
            "section": infer_section(text, chunk.get("section_title")),
            "token_count": chunk.get("token_count", 0),
            "page_start": chunk.get("page_start", 1),
            "page_end": chunk.get("page_end", 1),
            "is_reference": is_reference_section(text),
            "query_enabled": not bool(chunk.get("is_excluded", False)),
            "quality_score": float(quality_score)
            if str(quality_score).strip().replace(".", "", 1).isdigit()
            else 0.0,
            "priority_rank": float(priority_rank)
            if str(priority_rank).strip().replace(".", "", 1).isdigit()
            else 0.0,
        }
        if include_e5_prefix_field:
            record["text_for_embedding"] = f"passage: {text}" if text else "passage:"
        domain_tags_flat = (chunk.get("domain_tags_flat") or "").strip()
        if domain_tags_flat:
            from .vector_store import normalize_domain_tags_field

            record["domain_tags"] = normalize_domain_tags_field(domain_tags_flat)
        records.append(record)
    return records


def prepare_ingestion_records(
    document_id: str,
    filename: str,
    chunks: list[dict],
    workflow_id: str | None = None,
    name_gu: str | None = None,
    name_en: str | None = None,
    description: str | None = None,
    instance: str | None = None,
) -> list[dict]:
    """Public helper for constructing a vector-store ingestion payload."""
    return prepare_records(
        document_id,
        filename,
        chunks,
        workflow_id=workflow_id,
        name_gu=name_gu,
        name_en=name_en,
        description=description,
        instance=instance,
    )
