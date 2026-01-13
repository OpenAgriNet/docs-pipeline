"""
Temporal activities for the OCR pipeline.
Each activity is a retryable unit of work.
"""

import os
import re
import base64
import hashlib
from pathlib import Path
from datetime import datetime

import tiktoken
from mistralai import Mistral
from langchain_text_splitters import RecursiveCharacterTextSplitter
from temporalio import activity

from .models import PageData, ChunkData


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
    """
    activity.logger.info(f"Running OCR on {filepath}")

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not set")

    client = Mistral(api_key=api_key)

    with open(filepath, 'rb') as f:
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


@activity.defn
async def create_chunks(
    pages: list[dict],
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100
) -> list[dict]:
    """
    Create chunks from pages.
    Returns list of chunk data dicts.
    """
    activity.logger.info(f"Creating chunks from {len(pages)} pages")

    # Combine all page markdown (use edited if available)
    full_text = "\n\n".join(
        p.get("edited_markdown") or p.get("original_markdown", "")
        for p in pages
    )

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=count_tokens,
        separators=["\n\n", "\n", ".", " ", ""]
    )

    raw_chunks = splitter.split_text(full_text)

    chunks = []
    chunk_num = 1
    for chunk_text in raw_chunks:
        token_count = count_tokens(chunk_text)
        if token_count < min_tokens:
            continue

        chunks.append({
            "chunk_number": chunk_num,
            "original_text": chunk_text,
            "edited_text": None,
            "token_count": token_count,
            "is_reviewed": False,
            "is_excluded": False,
            "reviewer_notes": None
        })
        chunk_num += 1

    activity.logger.info(f"Created {len(chunks)} chunks")
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
            "source": "documents"
        })

    activity.logger.info(f"Prepared {len(records)} records for ingestion")
    return records


@activity.defn
async def ingest_to_marqo(
    records: list[dict],
    marqo_url: str = "http://127.0.0.1:8882",
    index_name: str = "documents-index",
    batch_size: int = 10
) -> dict:
    """
    Ingest records to Marqo.
    Returns ingestion stats.
    """
    import marqo

    activity.logger.info(f"Ingesting {len(records)} records to Marqo")

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
            {"name": "text", "type": "text", "features": ["lexical_search"]}
        ],
        "tensorFields": ["text"]
    }

    try:
        mq.get_index(index_name)
    except:
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
