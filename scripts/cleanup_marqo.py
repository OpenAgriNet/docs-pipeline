"""
Post-processing script to clean up existing Marqo data.

Cleans:
1. Translation preambles (e.g., "Here is the translated text from...")
2. Marks reference/citation sections with is_reference flag

Defaults to dry-run. Pass ``--apply`` to write.

Usage:
    python scripts/cleanup_marqo.py
    python scripts/cleanup_marqo.py --apply [--index-name NAME]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.vector_store import VectorStoreError, default_physical_index, get_vector_store  # noqa: E402


def clean_translation_preamble(text: str) -> str:
    """Remove common LLM preambles from translation output."""
    if not text:
        return text

    result = text

    # Remove "Here is the translated text from **X** to English..." patterns (with markdown)
    result = re.sub(
        r"^Here is the translated text from \*\*[^*]+\*\* to English[^:]*:?\s*\n*-{0,3}\s*\n*",
        "", result, flags=re.IGNORECASE
    )
    # Remove "Here is the translated text from X to English..." patterns (plain)
    result = re.sub(
        r"^Here is the translated text from [^:]+?:\s*\n*-{0,3}\s*\n*",
        "", result, flags=re.IGNORECASE
    )
    # Remove "Here is the translated text..." without language specification
    result = re.sub(
        r"^Here is the translated text[^:]*:?\s*\n*-{0,3}\s*\n*",
        "", result, flags=re.IGNORECASE
    )
    # Remove standalone "Here is the translation:" lines
    result = re.sub(
        r"^Here is the (?:English )?translation[^:]*:?\s*\n*",
        "", result, flags=re.IGNORECASE | re.MULTILINE
    )
    # Remove "Here is the translated text with all formatting preserved:" pattern
    result = re.sub(
        r"^Here is the translated text with[^:]+:?\s*\n*-{0,3}\s*\n*",
        "", result, flags=re.IGNORECASE
    )

    # Remove other common prefixes
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

    # Remove trailing "---" separators
    result = re.sub(r"\n*-{3,}\s*$", "", result)

    return result.strip()


def is_reference_section(text: str) -> bool:
    """
    Detect if text is primarily a reference/bibliography section.
    Returns True if the text appears to be citations/references.
    """
    if not text or len(text) < 50:
        return False

    # Common reference section headers
    ref_headers = [
        r'^\s*#{1,3}\s*(?:references|bibliography|citations|works cited|literature cited)\s*$',
        r'^\s*\*{1,2}(?:references|bibliography)\*{1,2}\s*$',
    ]
    for pattern in ref_headers:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return True

    # Count citation patterns
    lines = text.split('\n')
    total_lines = len([line for line in lines if line.strip()])
    if total_lines == 0:
        return False

    citation_patterns = [
        # Numbered citations: "1. Author, A. (2020)..." or "1. Author A..."
        r'^\s*\d{1,3}[\.\)]\s+[A-Z][a-z]+[\s,].*(?:\d{4}|\(\d{4}\))',
        # DOI patterns
        r'doi[:\s]*10\.\d{4,}',
        # Journal patterns
        r'(?:J\.|Journal|Int\.|Proceedings|Trans\.).*\d{4}',
        # Year in parentheses at end of line (common in citations)
        r'\(\d{4}\)\s*$',
        # "et al." pattern common in academic citations
        r'\bet\s+al\b',
        # Volume/issue patterns: "Vol. 12" or "12(3):"
        r'(?:Vol\.?\s*\d+|\d+\s*\(\d+\)\s*:)',
        # Page ranges: "pp. 123-456" or ": 123-456"
        r'(?:pp?\.?\s*\d+[-–]\d+|:\s*\d+[-–]\d+)',
    ]

    citation_line_count = 0
    for line in lines:
        if not line.strip():
            continue
        for pattern in citation_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                citation_line_count += 1
                break

    # If more than 40% of lines match citation patterns, it's likely a reference section
    citation_ratio = citation_line_count / total_lines
    return citation_ratio > 0.4


def process_document(doc: dict) -> tuple[dict, bool, bool]:
    """
    Process a single document: clean preamble, detect references.
    Returns (updated_doc, was_cleaned, is_reference)
    """
    text = doc.get("text", "")
    original_text = text

    # Clean translation preamble
    cleaned_text = clean_translation_preamble(text)
    was_cleaned = cleaned_text != original_text

    # Detect if reference section
    is_ref = is_reference_section(cleaned_text)

    # Build updated document
    updated_doc = {
        "_id": doc["_id"],
        "doc_id": doc.get("doc_id", ""),
        "name": doc.get("name", ""),
        "source": doc.get("source", "document_pipeline"),
        "chunk_num": doc.get("chunk_num", 0),
        "token_count": doc.get("token_count", 0),
        "page_start": doc.get("page_start", 1),
        "page_end": doc.get("page_end", 1),
        "text": cleaned_text,
        "is_reference": is_ref,
    }

    return updated_doc, was_cleaned, is_ref


def ensure_schema_has_is_reference(store, index_name: str) -> bool:
    """Check if index has is_reference field."""
    try:
        field_names = store.field_names(index_name)
    except VectorStoreError as error:
        print(f"Error checking schema: {error}")
        return False

    if "is_reference" not in field_names:
        print("WARNING: Index schema doesn't have 'is_reference' field.")
        print("The field will be added when documents are re-indexed.")
        print("For full filtering support, you may need to recreate the index.")
        return False
    return True


def run_cleanup(
    marqo_url: str = "http://localhost:8882",
    index_name: str = "documents-index",
    batch_size: int = 100,
    apply: bool = False,
):
    """
    Run cleanup on all documents in the Marqo index.
    """
    store = get_vector_store(url=marqo_url)
    print(f"Connecting to Marqo at {store.url}")

    # Check schema
    ensure_schema_has_is_reference(store, index_name)

    # Get index stats
    try:
        stats = store.get_stats(index_name)
        total_docs = stats.get("numberOfDocuments", 0)
        print(f"Index '{index_name}' has {total_docs} documents")
    except VectorStoreError as error:
        print(f"Error getting index stats: {error}")
        return

    if total_docs == 0:
        print("No documents to process")
        return

    # Process documents in batches
    cleaned_count = 0
    reference_count = 0
    processed_count = 0
    updated_docs: list[dict] = []

    # Fetch all documents using search with empty query
    offset = 0

    while offset < total_docs:
        print(f"\nFetching documents {offset} to {offset + batch_size}...")

        try:
            results = store.search(
                index_name,
                q="",
                limit=batch_size,
                offset=offset,
                show_highlights=False,
            )

            hits = results.get("hits", [])
            if not hits:
                break

            for doc in hits:
                updated_doc, was_cleaned, is_ref = process_document(doc)
                processed_count += 1

                if was_cleaned:
                    cleaned_count += 1
                    if apply:
                        print(f"  Cleaned preamble from: {doc.get('name', 'unknown')} chunk {doc.get('chunk_num', '?')}")

                if is_ref:
                    reference_count += 1
                    if apply:
                        print(f"  Marked as reference: {doc.get('name', 'unknown')} chunk {doc.get('chunk_num', '?')}")

                if apply and (was_cleaned or is_ref):
                    updated_docs.append(updated_doc)

            # Batch update to Marqo
            if apply and updated_docs and len(updated_docs) >= batch_size:
                print(f"  Updating {len(updated_docs)} documents in Marqo...")
                store.add_documents(index_name, updated_docs, batch_size=batch_size)
                updated_docs = []

            offset += batch_size

        except Exception as error:
            print(f"Error processing batch at offset {offset}: {error}")
            break

    # Final batch update
    if apply and updated_docs:
        print(f"\nUpdating final {len(updated_docs)} documents in Marqo...")
        store.add_documents(index_name, updated_docs, batch_size=batch_size)

    # Summary
    print(f"\n{'='*50}")
    print(f"CLEANUP SUMMARY {'(DRY RUN)' if not apply else ''}")
    print(f"{'='*50}")
    print(f"Total documents processed: {processed_count}")
    print(f"Documents with preamble cleaned: {cleaned_count}")
    print(f"Documents marked as references: {reference_count}")

    if not apply:
        print("\nThis was a dry run. No changes were made.")
        print("Run with --apply to write changes.")


def main():
    parser = argparse.ArgumentParser(description="Clean up Marqo index data")
    parser.add_argument("--marqo-url", default="http://localhost:8882", help="Marqo URL")
    parser.add_argument(
        "--index-name",
        default=default_physical_index(),
        help="Index name",
    )
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write updates to Marqo (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=argparse.SUPPRESS,  # older invocations remain dry-run
    )

    args = parser.parse_args()
    apply = bool(args.apply) and not bool(args.dry_run)

    run_cleanup(
        marqo_url=args.marqo_url,
        index_name=args.index_name,
        batch_size=args.batch_size,
        apply=apply,
    )


if __name__ == "__main__":
    main()
