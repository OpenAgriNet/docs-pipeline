"""
Temporal activities for the OCR pipeline.
Each activity is a retryable unit of work.
"""

import os
import re
import base64
import hashlib
import tempfile
from pathlib import Path
from datetime import datetime

import tiktoken
from mistralai import Mistral
from minio import Minio
from langchain_text_splitters import RecursiveCharacterTextSplitter
from temporalio import activity

from .models import PageData, ChunkData


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
        secure=False
    )


def download_from_minio(minio_path: str) -> str:
    """Download file from MinIO and return local temp path."""
    # Parse minio://bucket/object/path
    path = minio_path.replace("minio://", "")
    parts = path.split("/", 1)
    bucket = parts[0]
    object_name = parts[1] if len(parts) > 1 else ""

    client = get_minio_client()

    # Create temp file
    suffix = Path(object_name).suffix
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp_file.name
    temp_file.close()

    # Download
    client.fget_object(bucket, object_name, temp_path)
    return temp_path


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
    text = text.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


def clean_latex_notation(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = re.sub(r'\[\^[0-9]+\]', '', text)
    text = re.sub(r'\$\s*\{\s*\}\s*\^\{[0-9]+\}\s*\$', '', text)
    text = re.sub(r'\$\s*\^\{[0-9]+\}\s*\$', '', text)
    text = re.sub(r'\$\s*\$', '', text)
    text = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', '', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = re.sub(r'[\\{}]', '', text)
    return text


def format_table_content(text: str) -> str:
    if not isinstance(text, str):
        return text
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        if re.match(r'^[\s\|]*$', line):
            continue
        if re.match(r'^[\s\|\-\:]*$', line):
            continue
        line = re.sub(r'\|\s*\|', '|', line)
        line = re.sub(r'^\|\s*', '', line)
        line = re.sub(r'\s*\|$', '', line)
        line = re.sub(r'\s+', ' ', line).strip()
        if line:
            cleaned_lines.append(line)
    return '\n'.join(cleaned_lines)


def clean_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = clean_html_tags(text)
    text = clean_latex_notation(text)
    text = format_table_content(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
    text = re.sub(r'\n ', '\n', text)
    text = re.sub(r' \n', '\n', text)
    return text.strip()


# =============================================================================
# Activities
# =============================================================================

@activity.defn
async def run_ocr(filepath: str) -> list[dict]:
    """
    Run OCR on a PDF file.
    Returns list of page data dicts.
    Supports both local files and minio:// URIs.
    """
    activity.logger.info(f"Running OCR on {filepath}")

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not set")

    client = Mistral(api_key=api_key)

    # Handle MinIO paths
    local_path = filepath
    cleanup_temp = False
    if filepath.startswith("minio://"):
        local_path = download_from_minio(filepath)
        cleanup_temp = True
        activity.logger.info(f"Downloaded from MinIO to {local_path}")

    try:
        with open(local_path, 'rb') as f:
            base64_content = base64.b64encode(f.read()).decode('utf-8')

        response = client.ocr.process(
            model="mistral-ocr-latest",
            document={
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{base64_content}"
            },
            include_image_base64=False,
            image_limit=0
        )

        pages = []
        for i, page in enumerate(response.pages, 1):
            cleaned_md = clean_text(page.markdown)
            pages.append({
                "page_number": i,
                "original_markdown": cleaned_md,
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None
            })

        activity.logger.info(f"OCR complete: {len(pages)} pages")
        return pages

    finally:
        # Cleanup temp file if downloaded from MinIO
        if cleanup_temp and os.path.exists(local_path):
            os.remove(local_path)


@activity.defn
async def create_chunks(
    pages: list[dict],
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100
) -> list[dict]:
    """
    Create chunks from pages with page range tracking.
    Returns list of chunk data dicts with page_start and page_end.
    """
    activity.logger.info(f"Creating chunks from {len(pages)} pages")

    # Build combined text while tracking page boundaries
    page_boundaries = []  # List of (start_char, end_char, page_number)
    combined_parts = []
    current_pos = 0

    for p in pages:
        # Use translated text if available, otherwise original
        # Priority: edited_translation > translated_markdown > edited_markdown > original_markdown
        page_text = (
            p.get("edited_translation") or
            p.get("translated_markdown") or
            p.get("edited_markdown") or
            p.get("original_markdown", "")
        )
        page_num = p.get("page_number", 1)

        if combined_parts:
            # Add separator between pages
            combined_parts.append("\n\n")
            current_pos += 2

        start_pos = current_pos
        combined_parts.append(page_text)
        current_pos += len(page_text)
        end_pos = current_pos

        page_boundaries.append((start_pos, end_pos, page_num))

    full_text = "".join(combined_parts)

    # Use langchain splitter with character tracking
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=count_tokens,
        separators=["\n\n", "\n", ".", " ", ""]
    )

    raw_chunks = splitter.split_text(full_text)

    def find_page_range(chunk_text: str) -> tuple[int, int]:
        """Find which pages a chunk spans by locating it in the full text."""
        # Find chunk position in combined text
        chunk_start = full_text.find(chunk_text)
        if chunk_start == -1:
            # Fallback: try to find a significant portion
            search_text = chunk_text[:min(200, len(chunk_text))]
            chunk_start = full_text.find(search_text)
            if chunk_start == -1:
                return (1, len(pages))  # Fallback to full range

        chunk_end = chunk_start + len(chunk_text)

        # Find pages that overlap with this chunk
        page_start = None
        page_end = None

        for start, end, page_num in page_boundaries:
            # Check if chunk overlaps with this page
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

        chunks.append({
            "chunk_number": chunk_num,
            "original_text": chunk_text,
            "edited_text": None,
            "token_count": token_count,
            "page_start": page_start,
            "page_end": page_end,
            "is_reviewed": False,
            "is_excluded": False,
            "reviewer_notes": None
        })
        chunk_num += 1

    activity.logger.info(f"Created {len(chunks)} chunks with page tracking")
    return chunks


@activity.defn
async def prepare_for_ingestion(
    document_id: str,
    filename: str,
    chunks: list[dict]
) -> list[dict]:
    """
    Prepare chunks for Marqo ingestion.
    Returns list of Marqo-ready documents.
    """
    activity.logger.info(f"Preparing {len(chunks)} chunks for ingestion")

    doc_hash = hashlib.md5(document_id.encode()).hexdigest()
    name = filename.replace(".pdf", "")

    records = []
    for chunk in chunks:
        # Skip excluded chunks
        if chunk.get("is_excluded", False):
            continue

        text = chunk.get("edited_text") or chunk.get("original_text", "")
        chunk_num = chunk.get("chunk_number", 0)

        records.append({
            "_id": hashlib.md5(f"{doc_hash}_{chunk_num}_{text[:50]}".encode()).hexdigest(),
            "doc_id": doc_hash,
            "name": name,
            "text": text,
            "chunk_num": chunk_num,
            "token_count": chunk.get("token_count", 0),
            "page_start": chunk.get("page_start", 1),
            "page_end": chunk.get("page_end", 1),
            "source": "documents"
        })

    activity.logger.info(f"Prepared {len(records)} records for ingestion")
    return records


@activity.defn
async def ingest_to_marqo(
    records: list[dict],
    marqo_url: str = None,
    index_name: str = "documents-index",
    batch_size: int = 10
) -> dict:
    """
    Ingest records to Marqo.
    Returns ingestion stats.
    """
    import os
    import marqo

    # Use environment variable if marqo_url not provided or empty
    if not marqo_url:
        marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")

    activity.logger.info(f"Ingesting {len(records)} records to Marqo at {marqo_url}")

    mq = marqo.Client(url=marqo_url)

    # Ensure index exists
    settings = {
        "type": "structured",
        "vectorNumericType": "float",
        "model": "hf/multilingual-e5-large",
        "normalizeEmbeddings": False,
        "textPreprocessing": {
            "splitLength": 3,
            "splitOverlap": 1,
            "splitMethod": "sentence"
        },
        "allFields": [
            {"name": "doc_id", "type": "text", "features": ["filter"]},
            {"name": "name", "type": "text", "features": ["filter", "lexical_search"]},
            {"name": "source", "type": "text", "features": ["filter"]},
            {"name": "chunk_num", "type": "int", "features": ["filter"]},
            {"name": "token_count", "type": "int", "features": ["filter"]},
            {"name": "page_start", "type": "int", "features": ["filter"]},
            {"name": "page_end", "type": "int", "features": ["filter"]},
            {"name": "text", "type": "text", "features": ["lexical_search"]}
        ],
        "tensorFields": ["text"]
    }

    try:
        mq.get_index(index_name)
    except Exception:
        mq.create_index(index_name, settings_dict=settings)
        activity.logger.info(f"Created index: {index_name}")

    index = mq.index(index_name)

    # Batch ingest
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        index.add_documents(batch)

    stats = index.get_stats()
    activity.logger.info(f"Ingestion complete: {stats}")

    return {"records_ingested": len(records), "index_stats": stats}


@activity.defn
async def update_document_state(
    workflow_id: str,
    stage: str,
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: str = None
) -> dict:
    """
    Update document state in SQLite.
    Called from workflow to persist state during long activities.
    """
    from . import db

    activity.logger.info(f"Updating state for {workflow_id}: stage={stage}")

    db.update_document_stage(
        workflow_id=workflow_id,
        stage=stage,
        page_count=page_count,
        chunk_count=chunk_count,
        error_message=error_message
    )

    return {"updated": True, "stage": stage}


@activity.defn
async def persist_document_content(
    workflow_id: str,
    pages: list[dict],
    chunks: list[dict]
) -> dict:
    """
    Persist pages and chunks to SQLite for post-workflow access.
    Called when workflow completes to enable editing after completion.
    """
    from . import db

    activity.logger.info(f"Persisting content for {workflow_id}: {len(pages)} pages, {len(chunks)} chunks")

    db.save_pages(workflow_id, pages)
    db.save_chunks(workflow_id, chunks)

    return {"persisted": True, "pages": len(pages), "chunks": len(chunks)}


@activity.defn
async def detect_and_translate_pages(
    pages: list[dict],
    target_language: str = "en",
    source_language: str = None  # Auto-detect if None
) -> list[dict]:
    """
    Detect language and translate non-English pages.
    Uses lang-detect service for detection, Mistral for translation.
    Returns updated pages with translation fields.
    """
    import os
    import httpx
    from mistralai import Mistral

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not set")

    lang_detect_url = os.environ.get("LANG_DETECT_URL", "http://localhost:3001")
    client = Mistral(api_key=api_key)

    activity.logger.info(f"Processing {len(pages)} pages for translation")
    activity.logger.info(f"Using lang-detect service at {lang_detect_url}")

    # Translation prompt
    translate_prompt = """Translate the following text from {source_lang} to English.
Preserve all formatting, including markdown syntax, tables, and bullet points.
Maintain technical terminology accurately.
If there are proper nouns or names, keep them as-is or transliterate appropriately.

Original text:
{text}

English translation:"""

    # Detect languages line-by-line - if ANY line is non-English, translate the page
    detected_languages = {}

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        for i, page in enumerate(pages):
            text = page.get("edited_markdown") or page.get("original_markdown", "")
            if not text or len(text.strip()) < 20:
                detected_languages[i] = "en"
                continue

            # Split into lines and filter meaningful ones
            lines = [line.strip() for line in text.split('\n') if len(line.strip()) >= 10]
            if not lines:
                detected_languages[i] = "en"
                continue

            # Batch detect all lines for this page
            try:
                response = await http_client.post(
                    f"{lang_detect_url}/detect/batch",
                    json={"texts": lines}
                )
                response.raise_for_status()
                results = response.json().get("results", [])

                # Check if any line is non-English
                non_english_lang = None
                for result in results:
                    lang = result.get("language", "en").lower()
                    if lang != "en" and lang != "unknown":
                        non_english_lang = lang
                        activity.logger.info(f"Page {page.get('page_number')}: Found non-English line ({lang}): {result.get('text_preview', '')[:50]}")
                        break

                detected_languages[i] = non_english_lang if non_english_lang else "en"

            except Exception as e:
                activity.logger.warning(f"Lang-detect error for page {i}: {e}")
                detected_languages[i] = "en"

    # Update pages with detected languages and translate if needed
    translated_count = 0
    for i, page in enumerate(pages):
        if i in detected_languages:
            detected_lang = detected_languages[i]

            # Map common language names to ISO codes
            lang_map = {
                "english": "en", "hindi": "hi", "gujarati": "gu",
                "marathi": "mr", "tamil": "ta", "telugu": "te",
                "kannada": "kn", "malayalam": "ml", "punjabi": "pa",
                "bengali": "bn", "oriya": "or", "odia": "or"
            }
            detected_lang = lang_map.get(detected_lang.lower(), detected_lang.lower()[:2] if detected_lang else "en")

            page["detected_language"] = detected_lang
            activity.logger.info(f"Page {page.get('page_number')}: detected language = {detected_lang}")

            # Translate if not English
            if detected_lang != "en":
                text = page.get("edited_markdown") or page.get("original_markdown", "")
                activity.logger.info(f"Translating page {page.get('page_number')} from {detected_lang}")

                try:
                    translate_response = client.chat.complete(
                        model="mistral-large-latest",
                        messages=[{
                            "role": "user",
                            "content": translate_prompt.format(source_lang=detected_lang, text=text)
                        }],
                        max_tokens=8000
                    )

                    page["translated_markdown"] = translate_response.choices[0].message.content
                    translated_count += 1
                except Exception as e:
                    activity.logger.warning(f"Translation error for page {page.get('page_number')}: {e}")

    activity.logger.info(f"Translation complete: {translated_count} pages translated")
    return pages
