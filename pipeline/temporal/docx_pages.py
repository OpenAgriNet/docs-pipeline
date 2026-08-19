"""Native .docx text extraction — avoids render-to-PDF + OCR for text documents."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_to_pages(input_path: str, *, chars_per_page: int = 4000) -> list[dict]:
    """Extract paragraph text from a .docx and paginate for the pipeline."""
    paragraphs = _extract_docx_paragraphs(input_path)
    if not paragraphs:
        return [{
            "page_number": 1,
            "original_markdown": f"# {Path(input_path).name}\n\n(Empty document)",
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        }]

    body = "\n\n".join(paragraphs)
    pages: list[dict] = []
    page_num = 1
    for i in range(0, len(body), chars_per_page):
        chunk = body[i : i + chars_per_page].strip()
        if not chunk:
            continue
        pages.append({
            "page_number": page_num,
            "original_markdown": chunk,
            "edited_markdown": None,
            "is_reviewed": False,
            "reviewer_notes": None,
        })
        page_num += 1
    return pages or [{
        "page_number": 1,
        "original_markdown": f"# {Path(input_path).name}\n\n(Empty document)",
        "edited_markdown": None,
        "is_reviewed": False,
        "reviewer_notes": None,
    }]


def _extract_docx_paragraphs(input_path: str) -> list[str]:
    with zipfile.ZipFile(input_path) as zf:
        with zf.open("word/document.xml") as raw:
            root = ET.parse(raw).getroot()

    paragraphs: list[str] = []
    for para in root.iter(f"{_W_NS}p"):
        parts: list[str] = []
        for node in para.iter(f"{_W_NS}t"):
            if node.text:
                parts.append(node.text)
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return paragraphs
