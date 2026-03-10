"""
Temporal activities for the OCR pipeline.
Each activity is a retryable unit of work.
"""

import asyncio
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from threading import Lock

import httpx
import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter
from minio import Minio
from mistralai import Mistral
from temporalio import activity

SUPPORTED_INPUT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"
}
IMAGE_INPUT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
OFFICE_INPUT_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
DELIMITED_INPUT_EXTENSIONS = {".csv"}
NATIVE_SPREADSHEET_EXTENSIONS = {".xlsx"}

_doc_metadata_cache: dict[str, dict] = {}
_doc_descriptions_cache: dict[str, str] = {}
_metadata_loaded = False
_metadata_lock = Lock()


def get_minio_client():
    """Get MinIO client from environment. Credentials are required."""
    access_key = os.environ.get("MINIO_ACCESS_KEY")
    secret_key = os.environ.get("MINIO_SECRET_KEY")

    if not access_key or not secret_key:
        raise RuntimeError("MINIO_ACCESS_KEY and MINIO_SECRET_KEY environment variables are required")

    return Minio(
        os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        access_key=access_key,
        secret_key=secret_key,
        secure=False,
    )


def download_from_minio(minio_path: str) -> str:
    """Download file from MinIO and return local temp path."""
    path = minio_path.replace("minio://", "")
    parts = path.split("/", 1)
    bucket = parts[0]
    object_name = parts[1] if len(parts) > 1 else ""

    client = get_minio_client()

    suffix = Path(object_name).suffix
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp_file.name
    temp_file.close()

    client.fget_object(bucket, object_name, temp_path)
    return temp_path


def _resolve_local_path(filepath: str) -> tuple[str, bool]:
    """
    Resolve path into local file path.
    Returns (path, should_cleanup).
    """
    if filepath.startswith("minio://") or filepath.startswith("minio:/"):
        minio_path = filepath
        if filepath.startswith("minio:/") and not filepath.startswith("minio://"):
            minio_path = filepath.replace("minio:/", "minio://", 1)
        local_path = download_from_minio(minio_path)
        activity.logger.info(f"Downloaded from MinIO to {local_path}")
        return local_path, True
    return filepath, False


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
                with open(metadata_csv_path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        file_name = (row.get("File Name") or "").strip()
                        if not file_name or file_name.startswith("http://") or file_name.startswith("https://"):
                            continue
                        key = _normalize_filename(file_name)
                        _doc_metadata_cache[key] = {
                            "title_en": (row.get("Title (English)") or "").strip(),
                            "title_gu": (row.get("Title (Gujarati)") or "").strip(),
                            "doc_language": (row.get("Language (Gujarati / English)") or "").strip(),
                            "category_tags": (row.get("Category Tags (") or "").strip(),
                            "doc_short_description": (row.get("Description") or "").strip(),
                            "quality_score": (row.get("Quality(1-5)") or "").strip(),
                            "priority_rank": (row.get("Priority(1-5)") or "").strip(),
                            "ingestion_status": (row.get("Status ingested in the system") or "").strip(),
                        }
            except Exception as e:
                activity.logger.warning(f"Failed loading document metadata CSV: {e}")

        if os.path.exists(descriptions_jsonl_path):
            try:
                with open(descriptions_jsonl_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        key = _normalize_filename(obj.get("file", ""))
                        if key and obj.get("description"):
                            _doc_descriptions_cache[key] = str(obj["description"]).strip()
            except Exception as e:
                activity.logger.warning(f"Failed loading document descriptions JSONL: {e}")

        _metadata_loaded = True


def _get_doc_metadata(filename: str) -> dict:
    _load_metadata_once()
    return _doc_metadata_cache.get(_normalize_filename(filename), {})


def _get_doc_description(filename: str) -> str:
    _load_metadata_once()
    return _doc_descriptions_cache.get(_normalize_filename(filename), "")


def _convert_image_to_pdf(input_path: str, output_path: str) -> None:
    from PIL import Image, ImageOps

    with Image.open(input_path) as img:
        rgb_img = ImageOps.exif_transpose(img).convert("RGB")
        rgb_img.save(output_path, "PDF", resolution=300.0)


def _convert_office_to_pdf(input_path: str, output_dir: str) -> str:
    soffice_bin = shutil.which("soffice")
    if not soffice_bin:
        raise RuntimeError(
            "LibreOffice (soffice) is not installed. Install libreoffice to convert office files to PDF."
        )

    cmd = [
        soffice_bin,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        output_dir,
        input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to convert office document to PDF: {result.stderr.strip() or result.stdout.strip()}"
        )

    output_pdf = Path(output_dir) / f"{Path(input_path).stem}.pdf"
    if not output_pdf.exists():
        raise RuntimeError("Office-to-PDF conversion finished but output PDF was not found")
    return str(output_pdf)


def _ensure_pdf_input(local_path: str) -> tuple[str, bool]:
    """
    Ensure the given file path points to a PDF.
    Returns (pdf_path, should_cleanup).
    """
    ext = Path(local_path).suffix.lower()
    if ext not in SUPPORTED_INPUT_EXTENSIONS:
        raise ValueError(
            f"Unsupported file extension '{ext}'. Supported extensions: {sorted(SUPPORTED_INPUT_EXTENSIONS)}"
        )

    if ext == ".pdf":
        return local_path, False

    work_dir = tempfile.mkdtemp(prefix="doc_convert_")
    output_pdf = Path(work_dir) / f"{Path(local_path).stem}.pdf"

    if ext in IMAGE_INPUT_EXTENSIONS:
        _convert_image_to_pdf(local_path, str(output_pdf))
    elif ext in OFFICE_INPUT_EXTENSIONS:
        converted = _convert_office_to_pdf(local_path, work_dir)
        if converted != str(output_pdf):
            shutil.move(converted, output_pdf)
    else:
        raise ValueError(f"Unsupported conversion path for extension: {ext}")

    activity.logger.info(f"Converted {local_path} -> {output_pdf}")
    return str(output_pdf), True


def _csv_to_pages(input_path: str, rows_per_page: int = 80) -> list[dict]:
    import csv

    def _open_reader():
        last_err = None
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                f = open(input_path, "r", encoding=enc, newline="")
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except Exception:
                    dialect = csv.excel
                return f, csv.reader(f, dialect=dialect)
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Could not read CSV file: {last_err}")

    f, reader = _open_reader()
    try:
        rows = [[(c or "").strip() for c in r] for r in reader if any((c or "").strip() for c in r)]
    finally:
        f.close()

    if not rows:
        return [{
            "page_number": 1,
            "original_markdown": f"# {Path(input_path).name}\n\n(Empty CSV file)",
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        }]

    header = rows[0]
    body = rows[1:] if len(rows) > 1 else []

    pages: list[dict] = []
    page_num = 1
    for i in range(0, len(body), rows_per_page):
        batch = body[i:i + rows_per_page]
        lines = [f"# Spreadsheet Data: {Path(input_path).name}", "", f"Columns: {', '.join(header)}", ""]
        for row_idx, row in enumerate(batch, start=i + 1):
            pairs = []
            for col_i, val in enumerate(row):
                col = header[col_i] if col_i < len(header) and header[col_i] else f"col_{col_i+1}"
                pairs.append(f"{col}: {val}")
            if pairs:
                lines.append(f"- Row {row_idx}: " + " | ".join(pairs))
        pages.append({
            "page_number": page_num,
            "original_markdown": "\n".join(lines),
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        })
        page_num += 1
    return pages


def _xlsx_to_pages(input_path: str, rows_per_page: int = 80) -> list[dict]:
    from datetime import date, datetime
    from openpyxl import load_workbook

    def _cell_to_str(value) -> str:
        if value is None:
            return ""
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return str(value).strip()

    pages: list[dict] = []
    page_num = 1
    wb = load_workbook(filename=input_path, data_only=True, read_only=True)

    for sheet in wb.worksheets:
        header: list[str] | None = None
        batch: list[tuple[int, list[str]]] = []
        seen_rows = 0

        def emit_page(rows_batch: list[tuple[int, list[str]]]) -> None:
            nonlocal page_num
            if not rows_batch:
                return
            assert header is not None
            lines = [
                f"# Spreadsheet Data: {Path(input_path).name}",
                "",
                f"Sheet: {sheet.title}",
                f"Columns: {', '.join(header)}",
                "",
            ]
            for excel_row_num, row in rows_batch:
                pairs = []
                for col_i, val in enumerate(row):
                    col = header[col_i] if col_i < len(header) and header[col_i] else f"col_{col_i+1}"
                    pairs.append(f"{col}: {val}")
                if pairs:
                    lines.append(f"- Row {excel_row_num}: " + " | ".join(pairs))
            pages.append({
                "page_number": page_num,
                "original_markdown": "\n".join(lines),
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None,
            })
            page_num += 1

        for excel_row_num, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            values = [_cell_to_str(v) for v in row]
            if not any(values):
                continue
            seen_rows += 1

            if header is None:
                header = values
                continue

            batch.append((excel_row_num, values))
            if len(batch) >= rows_per_page:
                emit_page(batch)
                batch = []

        if header is not None:
            emit_page(batch)
        elif seen_rows == 0:
            pages.append({
                "page_number": page_num,
                "original_markdown": f"# Spreadsheet Data: {Path(input_path).name}\n\nSheet: {sheet.title}\n\n(Empty sheet)",
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None,
            })
            page_num += 1

    wb.close()

    if not pages:
        return [{
            "page_number": 1,
            "original_markdown": f"# Spreadsheet Data: {Path(input_path).name}\n\n(Empty workbook)",
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        }]
    return pages


# =============================================================================
# Text Processing Utilities
# =============================================================================

def count_tokens(text: str, model: str = "cl100k_base") -> int:
    if not text:
        return 0
    encoder = tiktoken.get_encoding(model)
    return len(encoder.encode(str(text), disallowed_special=()))


def clean_html_tags(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


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
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if re.match(r"^[\s\|]*$", line):
            continue
        if re.match(r"^[\s\|\-\:]*$", line):
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
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n\s*\n\s*\n", "\n\n", text)
    text = re.sub(r"\n ", "\n", text)
    text = re.sub(r" \n", "\n", text)
    return text.strip()


def is_reference_section(text: str) -> bool:
    """
    Detect if text is primarily a reference/bibliography section.
    Returns True if the text appears to be citations/references.
    """
    if not text or len(text) < 50:
        return False

    ref_headers = [
        r"^\s*#{1,3}\s*(?:references|bibliography|citations|works cited|literature cited)\s*$",
        r"^\s*\*{1,2}(?:references|bibliography)\*{1,2}\s*$",
    ]
    for pattern in ref_headers:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return True

    lines = text.split("\n")
    total_lines = len([l for l in lines if l.strip()])
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

    citation_line_count = 0
    for line in lines:
        if not line.strip():
            continue
        for pattern in citation_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                citation_line_count += 1
                break

    return (citation_line_count / total_lines) > 0.4


def _ocr_pdf(local_pdf_path: str) -> list[dict]:
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not set")

    client = Mistral(api_key=api_key)

    with open(local_pdf_path, "rb") as f:
        base64_content = base64.b64encode(f.read()).decode("utf-8")

    response = client.ocr.process(
        model="mistral-ocr-latest",
        document={
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{base64_content}",
        },
        include_image_base64=False,
        image_limit=0,
    )

    pages = []
    for i, page in enumerate(response.pages, 1):
        cleaned_md = clean_text(page.markdown)
        pages.append(
            {
                "page_number": i,
                "original_markdown": cleaned_md,
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None,
            }
        )

    activity.logger.info(f"OCR complete: {len(pages)} pages")
    return pages


def _build_chunks_from_pages(
    pages: list[dict],
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
) -> list[dict]:
    page_boundaries = []
    combined_parts = []
    current_pos = 0

    for p in pages:
        page_text = (
            p.get("edited_translation")
            or p.get("translated_markdown")
            or p.get("edited_markdown")
            or p.get("original_markdown", "")
        )
        page_num = p.get("page_number", 1)

        if combined_parts:
            combined_parts.append("\n\n")
            current_pos += 2

        start_pos = current_pos
        combined_parts.append(page_text)
        current_pos += len(page_text)
        end_pos = current_pos

        page_boundaries.append((start_pos, end_pos, page_num))

    full_text = "".join(combined_parts)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=count_tokens,
        separators=["\n\n", "\n", ".", " ", ""],
    )

    raw_chunks = splitter.split_text(full_text)

    def find_page_range(chunk_text: str) -> tuple[int, int]:
        chunk_start = full_text.find(chunk_text)
        if chunk_start == -1:
            search_text = chunk_text[: min(200, len(chunk_text))]
            chunk_start = full_text.find(search_text)
            if chunk_start == -1:
                return (1, len(pages))

        chunk_end = chunk_start + len(chunk_text)
        page_start = None
        page_end = None

        for start, end, page_num in page_boundaries:
            if chunk_start < end and chunk_end > start:
                if page_start is None:
                    page_start = page_num
                page_end = page_num

        return (page_start or 1, page_end or len(pages))

    chunks = []
    chunk_num = 1
    for chunk_text in raw_chunks:
        token_count = count_tokens(chunk_text)
        if token_count < min_tokens:
            continue

        page_start, page_end = find_page_range(chunk_text)

        chunks.append(
            {
                "chunk_number": chunk_num,
                "original_text": chunk_text,
                "edited_text": None,
                "token_count": token_count,
                "page_start": page_start,
                "page_end": page_end,
                "is_reviewed": False,
                "is_excluded": False,
                "reviewer_notes": None,
            }
        )
        chunk_num += 1

    return chunks


def clean_text_for_ingestion(text: str) -> str:
    """Clean translation preambles from text before ingestion."""
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


def _prepare_records(
    document_id: str,
    filename: str,
    chunks: list[dict],
    name_gu: str | None = None,
    name_en: str | None = None,
    description: str | None = None,
    include_e5_prefix_field: bool = True,
) -> list[dict]:
    metadata = _get_doc_metadata(filename)
    llm_doc_description = _get_doc_description(filename)

    doc_hash = hashlib.md5(document_id.encode()).hexdigest()
    default_name = filename.replace(".pdf", "").replace(".PDF", "")
    name_gu = name_gu or metadata.get("title_gu") or default_name
    name_en = name_en or metadata.get("title_en") or default_name
    category_tags = metadata.get("category_tags", "")
    quality_score = metadata.get("quality_score", "")
    priority_rank = metadata.get("priority_rank", "")
    ingestion_status = metadata.get("ingestion_status", "")
    doc_language = metadata.get("doc_language", "")
    short_description = metadata.get("doc_short_description", "")
    effective_description = description or llm_doc_description or short_description

    records = []
    for chunk in chunks:
        if chunk.get("is_excluded", False):
            continue

        raw_text = chunk.get("edited_text") or chunk.get("original_text", "")
        chunk_num = chunk.get("chunk_number", 0)
        text = clean_text_for_ingestion(raw_text)
        is_ref = is_reference_section(text)

        record = {
            "_id": hashlib.md5(f"{doc_hash}_{chunk_num}_{text[:50]}".encode()).hexdigest(),
            "doc_id": doc_hash,
            "type": "document",
            "source": "documents",
            "filename": filename,
            "name_gu": name_gu,
            "name_en": name_en,
            "title_en": metadata.get("title_en", ""),
            "title_gu": metadata.get("title_gu", ""),
            "doc_language": doc_language,
            "category_tags": category_tags,
            "doc_short_description": short_description,
            "doc_llm_description": llm_doc_description,
            "ingestion_status": ingestion_status,
            "description": effective_description,
            "text": text,
            "chunk_num": chunk_num,
            "token_count": chunk.get("token_count", 0),
            "page_start": chunk.get("page_start", 1),
            "page_end": chunk.get("page_end", 1),
            "is_reference": is_ref,
            "quality_score": float(quality_score) if str(quality_score).strip().replace(".", "", 1).isdigit() else 0.0,
            "priority_rank": float(priority_rank) if str(priority_rank).strip().replace(".", "", 1).isdigit() else 0.0,
        }
        if include_e5_prefix_field:
            record["text_for_embedding"] = f"passage: {text}" if text else "passage:"
        records.append(record)

    return records


def _passage_schema_field_names() -> set[str]:
    """Field names for the canonical passage schema (E5 text_for_embedding + full metadata)."""
    settings = _marqo_settings(use_tensor_prefix_field=True)
    return {f.get("name") for f in settings.get("allFields", []) if isinstance(f, dict) and f.get("name")}


def _marqo_settings(use_tensor_prefix_field: bool = True) -> dict:
    tensor_field = "text_for_embedding" if use_tensor_prefix_field else "text"
    all_fields = [
        {"name": "doc_id", "type": "text", "features": ["filter"]},
        {"name": "type", "type": "text", "features": ["filter"]},
        {"name": "source", "type": "text", "features": ["filter"]},
        {"name": "filename", "type": "text", "features": ["filter"]},
        {"name": "name_gu", "type": "text", "features": ["filter"]},
        {"name": "name_en", "type": "text", "features": ["filter"]},
        {"name": "title_en", "type": "text", "features": ["filter"]},
        {"name": "title_gu", "type": "text", "features": ["filter"]},
        {"name": "doc_language", "type": "text", "features": ["filter"]},
        {"name": "category_tags", "type": "text", "features": ["filter"]},
        {"name": "doc_short_description", "type": "text", "features": ["filter"]},
        {"name": "doc_llm_description", "type": "text", "features": ["filter"]},
        {"name": "ingestion_status", "type": "text", "features": ["filter"]},
        {"name": "description", "type": "text", "features": ["lexical_search"]},
        {"name": "chunk_num", "type": "int", "features": ["filter"]},
        {"name": "token_count", "type": "int", "features": ["filter"]},
        {"name": "page_start", "type": "int", "features": ["filter"]},
        {"name": "page_end", "type": "int", "features": ["filter"]},
        {"name": "is_reference", "type": "bool", "features": ["filter"]},
        {"name": "quality_score", "type": "float", "features": ["filter"]},
        {"name": "priority_rank", "type": "float", "features": ["filter"]},
        {"name": "text", "type": "text", "features": ["lexical_search"]},
        {"name": "priority", "type": "float", "features": ["score_modifier", "filter"]},
    ]
    if use_tensor_prefix_field:
        all_fields.append({"name": "text_for_embedding", "type": "text"})

    return {
        "type": "structured",
        "vectorNumericType": "float",
        "model": "hf/multilingual-e5-large",
        "normalizeEmbeddings": False,
        "textPreprocessing": {"splitLength": 3, "splitOverlap": 1, "splitMethod": "sentence"},
        "allFields": all_fields,
        "tensorFields": [tensor_field],
    }


async def _detect_and_translate_impl(
    pages: list[dict],
    target_language: str = "en",
    source_language: str | None = None,
) -> list[dict]:
    del target_language, source_language

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not set")

    lang_detect_url = os.environ.get("LANG_DETECT_URL", "http://localhost:3001")
    client = Mistral(api_key=api_key)

    activity.logger.info(f"Processing {len(pages)} pages for translation")
    activity.logger.info(f"Using lang-detect service at {lang_detect_url}")

    translate_prompt = """Translate the following text from {source_lang} to English.
Preserve all formatting, including markdown syntax, tables, and bullet points.
Maintain technical terminology accurately.
If there are proper nouns or names, keep them as-is or transliterate appropriately.
Do NOT include any preamble or introduction - start directly with the translated content.

Original text:
{text}"""

    def clean_translation(text: str) -> str:
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

    detected_languages = {}

    async def detect_languages():
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            for i, page in enumerate(pages):
                text = page.get("edited_markdown") or page.get("original_markdown", "")
                if not text or len(text.strip()) < 20:
                    detected_languages[i] = "en"
                    continue

                lines = [line.strip() for line in text.split("\n") if len(line.strip()) >= 10]
                if not lines:
                    detected_languages[i] = "en"
                    continue

                try:
                    response = await http_client.post(
                        f"{lang_detect_url}/detect/batch",
                        json={"texts": lines},
                    )
                    response.raise_for_status()
                    results = response.json().get("results", [])

                    non_english_lang = None
                    for result in results:
                        lang = result.get("language", "en").lower()
                        if lang != "en" and lang != "unknown":
                            non_english_lang = lang
                            activity.logger.info(
                                f"Page {page.get('page_number')}: Found non-English content, detected language: {lang}"
                            )
                            break

                    detected_languages[i] = non_english_lang if non_english_lang else "en"
                except Exception as e:
                    activity.logger.warning(f"Lang-detect error for page {i}: {type(e).__name__}: {e}")
                    detected_languages[i] = "en"

    await detect_languages()

    lang_map = {
        "english": "en",
        "hindi": "hi",
        "gujarati": "gu",
        "marathi": "mr",
        "tamil": "ta",
        "telugu": "te",
        "kannada": "kn",
        "malayalam": "ml",
        "punjabi": "pa",
        "bengali": "bn",
        "oriya": "or",
        "odia": "or",
    }

    pages_to_translate = []
    for i, page in enumerate(pages):
        if i in detected_languages:
            detected_lang = detected_languages[i]
            detected_lang = lang_map.get(detected_lang.lower(), detected_lang.lower()[:2] if detected_lang else "en")
            page["detected_language"] = detected_lang
            if detected_lang != "en":
                pages_to_translate.append((i, page, detected_lang))

    activity.logger.info(f"Found {len(pages_to_translate)} pages needing translation")

    semaphore = asyncio.Semaphore(5)

    async def translate_page(idx: int, page: dict, lang: str) -> tuple[int, str | None, str | None]:
        async with semaphore:
            text = page.get("edited_markdown") or page.get("original_markdown", "")
            activity.logger.info(f"Translating page {page.get('page_number')} from {lang}")
            try:
                def do_translate():
                    return client.chat.complete(
                        model="mistral-large-latest",
                        messages=[{"role": "user", "content": translate_prompt.format(source_lang=lang, text=text)}],
                        max_tokens=8000,
                    )

                translate_response = await asyncio.to_thread(do_translate)
                raw_translation = translate_response.choices[0].message.content
                return (idx, clean_translation(raw_translation), None)
            except Exception as e:
                activity.logger.warning(f"Translation error for page {page.get('page_number')}: {e}")
                return (idx, None, str(e))

    async def translate_all():
        if not pages_to_translate:
            return []
        tasks = [translate_page(i, p, lang) for i, p, lang in pages_to_translate]
        return await asyncio.gather(*tasks)

    results = await translate_all()
    translated_count = 0
    for idx, translation, _error in results:
        if translation:
            pages[idx]["translated_markdown"] = translation
            translated_count += 1

    activity.logger.info(f"Translation complete: {translated_count}/{len(pages_to_translate)} pages translated")
    return pages


# =============================================================================
# Activities
# =============================================================================

@activity.defn
async def run_ocr(filepath: str) -> list[dict]:
    """Run OCR on a supported file and return page dicts."""
    activity.logger.info(f"Running OCR on {filepath}")

    local_path, cleanup_local = _resolve_local_path(filepath)
    ext = Path(local_path).suffix.lower()
    pdf_path = local_path
    cleanup_pdf_dir = False

    try:
        if ext in DELIMITED_INPUT_EXTENSIONS:
            pages = _csv_to_pages(local_path)
            activity.logger.info(f"CSV parsed into {len(pages)} pages")
            return pages
        if ext in NATIVE_SPREADSHEET_EXTENSIONS:
            pages = _xlsx_to_pages(local_path)
            activity.logger.info(f"XLSX parsed into {len(pages)} pages")
            return pages
        pdf_path, cleanup_pdf_dir = _ensure_pdf_input(local_path)
        return _ocr_pdf(pdf_path)
    finally:
        if cleanup_pdf_dir:
            try:
                shutil.rmtree(Path(pdf_path).parent, ignore_errors=True)
            except Exception:
                pass
        if cleanup_local and os.path.exists(local_path):
            os.remove(local_path)


@activity.defn
async def run_ocr_and_store(workflow_id: str, filepath: str) -> dict:
    """Run OCR and persist pages to SQLite to avoid large Temporal payloads."""
    from . import db

    pages = await run_ocr(filepath)
    db.save_pages(workflow_id, pages)
    return {"page_count": len(pages)}


@activity.defn
async def create_chunks(
    pages: list[dict],
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
) -> list[dict]:
    """Create chunks from pages with page range tracking."""
    activity.logger.info(f"Creating chunks from {len(pages)} pages")
    chunks = _build_chunks_from_pages(
        pages=pages,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        min_tokens=min_tokens,
    )
    activity.logger.info(f"Created {len(chunks)} chunks with page tracking")
    return chunks


@activity.defn
async def create_chunks_from_db(
    workflow_id: str,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
) -> dict:
    """Create chunks from persisted pages and persist chunks in SQLite."""
    from . import db

    pages = db.get_pages(workflow_id)
    activity.logger.info(f"Creating chunks from DB pages for {workflow_id}: {len(pages)} pages")
    chunks = _build_chunks_from_pages(
        pages=pages,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        min_tokens=min_tokens,
    )
    db.save_chunks(workflow_id, chunks)
    return {"chunk_count": len(chunks)}


@activity.defn
async def prepare_for_ingestion(
    document_id: str,
    filename: str,
    chunks: list[dict],
    name_gu: str = None,
    name_en: str = None,
    description: str = None,
) -> list[dict]:
    """Prepare chunks for Marqo ingestion."""
    activity.logger.info(f"Preparing {len(chunks)} chunks for ingestion")
    records = _prepare_records(document_id, filename, chunks, name_gu=name_gu, name_en=name_en, description=description)
    activity.logger.info(f"Prepared {len(records)} records")
    return records


@activity.defn
async def ingest_to_marqo(
    records: list[dict],
    marqo_url: str = None,
    index_name: str = "documents-index",
    batch_size: int = 10,
) -> dict:
    """Ingest records to Marqo."""
    import marqo

    if not marqo_url:
        marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")

    activity.logger.info(f"Ingesting {len(records)} records to Marqo at {marqo_url}")
    mq = marqo.Client(url=marqo_url)

    settings = _marqo_settings(use_tensor_prefix_field=True)
    passage_fields = _passage_schema_field_names()

    index_exists = True
    try:
        mq.get_index(index_name)
    except Exception:
        index_exists = False

    if not index_exists:
        mq.create_index(index_name, settings_dict=settings)
        activity.logger.info(f"Created index: {index_name} (passage schema)")
    else:
        index = mq.index(index_name)
        try:
            index_settings = index.get_settings()
            tensor_fields = set(index_settings.get("tensorFields", [])) if isinstance(index_settings, dict) else set()
            index_field_names = {
                f.get("name") for f in (index_settings.get("allFields") or [])
                if isinstance(f, dict) and f.get("name")
            }
            has_passage_tensor = "text_for_embedding" in tensor_fields
            has_full_schema = passage_fields <= index_field_names
            if not (has_passage_tensor and has_full_schema):
                mq.delete_index(index_name)
                mq.create_index(index_name, settings_dict=settings)
                activity.logger.info(
                    f"Recreated index: {index_name} with passage schema (was missing text_for_embedding or fields)"
                )
        except Exception as e:
            activity.logger.warning("Could not verify index schema, recreating: %s", e)
            try:
                mq.delete_index(index_name)
            except Exception:
                pass
            mq.create_index(index_name, settings_dict=settings)
            activity.logger.info(f"Recreated index: {index_name} (passage schema)")

    index = mq.index(index_name)
    allowed_fields = passage_fields
    if allowed_fields:
        for i, record in enumerate(records):
            normalized = {"_id": record.get("_id")}
            for key, value in record.items():
                if key == "_id":
                    continue
                if key in allowed_fields:
                    normalized[key] = value
            records[i] = normalized

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        result = index.add_documents(batch)
        if result.get("errors"):
            errors = []
            for item in result.get("items") or []:
                if item.get("status") != 200:
                    errors.append({
                        "_id": item.get("_id"),
                        "status": item.get("status"),
                        "error": item.get("error"),
                        "message": item.get("message"),
                        "code": item.get("code"),
                    })
            activity.logger.error(
                "Marqo add_documents reported errors. First few: %s. Full result keys: %s",
                errors[:5],
                list(result.keys()),
            )
            if errors:
                raise RuntimeError(
                    f"Marqo add_documents failed for {len(errors)} doc(s). First error: {errors[0]}"
                )

    stats = index.get_stats()
    activity.logger.info(f"Ingestion complete: {stats}")

    return {
        "records_ingested": len(records),
        "index_stats": stats,
        "supports_prefixed_tensor_field": True,
    }


@activity.defn
async def ingest_document_from_db(
    workflow_id: str,
    document_id: str,
    filename: str,
    marqo_url: str = None,
    index_name: str = "documents-index",
    batch_size: int = 10,
) -> dict:
    """Prepare and ingest chunks directly from SQLite by workflow_id."""
    from . import db

    chunks = db.get_chunks(workflow_id, include_excluded=True)
    records = _prepare_records(document_id, filename, chunks)
    return await ingest_to_marqo(records, marqo_url=marqo_url, index_name=index_name, batch_size=batch_size)


@activity.defn
async def update_document_state(
    workflow_id: str,
    stage: str,
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: str = None,
) -> dict:
    """Update document state in SQLite."""
    from . import db

    activity.logger.info(f"Updating state for {workflow_id}: stage={stage}")
    db.update_document_stage(
        workflow_id=workflow_id,
        stage=stage,
        page_count=page_count,
        chunk_count=chunk_count,
        error_message=error_message,
    )
    return {"updated": True, "stage": stage}


@activity.defn
async def persist_document_content(workflow_id: str, pages: list[dict], chunks: list[dict]) -> dict:
    """Persist pages and chunks to SQLite."""
    from . import db

    activity.logger.info(f"Persisting content for {workflow_id}: {len(pages)} pages, {len(chunks)} chunks")
    db.save_pages(workflow_id, pages)
    db.save_chunks(workflow_id, chunks)
    return {"persisted": True, "pages": len(pages), "chunks": len(chunks)}


@activity.defn
async def detect_and_translate_pages(
    pages: list[dict],
    target_language: str = "en",
    source_language: str = None,
) -> list[dict]:
    """Detect language and translate non-English pages."""
    return await _detect_and_translate_impl(pages, target_language=target_language, source_language=source_language)


@activity.defn
async def detect_and_translate_pages_from_db(
    workflow_id: str,
    target_language: str = "en",
    source_language: str = None,
) -> dict:
    """Detect and translate pages loaded from SQLite; persist updated pages back to SQLite."""
    from . import db

    pages = db.get_pages(workflow_id)
    translated = await _detect_and_translate_impl(pages, target_language=target_language, source_language=source_language)
    db.save_pages(workflow_id, translated)
    translated_count = sum(1 for p in translated if p.get("translated_markdown"))
    return {"page_count": len(translated), "translated_count": translated_count}
