"""
Post-processing script to clean up existing Marqo data.

Cleans:
1. Translation preambles (e.g., "Here is the translated text from...")
2. Marks reference/citation sections with is_reference flag

Usage:
    python -m pipeline.cleanup_marqo [--dry-run] [--index-name NAME]
"""

import re
import argparse
import marqo
from typing import Optional


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
    total_lines = len([l for l in lines if l.strip()])
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
        "source": doc.get("source", "documents"),
        "chunk_num": doc.get("chunk_num", 0),
        "token_count": doc.get("token_count", 0),
        "page_start": doc.get("page_start", 1),
        "page_end": doc.get("page_end", 1),
        "text": cleaned_text,
        "is_reference": is_ref,
    }

    return updated_doc, was_cleaned, is_ref


def ensure_schema_has_is_reference(mq: marqo.Client, index_name: str):
    """Check if index has is_reference field, recreate if needed."""
    try:
        index_settings = mq.index(index_name).get_settings()
        fields = index_settings.get("allFields", [])
        field_names = [f.get("name") for f in fields]

        if "is_reference" not in field_names:
            print("WARNING: Index schema doesn't have 'is_reference' field.")
            print("The field will be added when documents are re-indexed.")
            print("For full filtering support, you may need to recreate the index.")
            return False
        return True
    except Exception as e:
        print(f"Error checking schema: {e}")
        return False


def run_cleanup(
    marqo_url: str = "http://localhost:8882",
    index_name: str = "documents-index",
    batch_size: int = 100,
    dry_run: bool = False
):
    """
    Run cleanup on all documents in the Marqo index.
    """
    print(f"Connecting to Marqo at {marqo_url}")
    mq = marqo.Client(url=marqo_url)

    # Check schema
    ensure_schema_has_is_reference(mq, index_name)

    # Get index stats
    try:
        stats = mq.index(index_name).get_stats()
        total_docs = stats.get("numberOfDocuments", 0)
        print(f"Index '{index_name}' has {total_docs} documents")
    except Exception as e:
        print(f"Error getting index stats: {e}")
        return

    if total_docs == 0:
        print("No documents to process")
        return

    # Process documents in batches
    cleaned_count = 0
    reference_count = 0
    processed_count = 0
    updated_docs = []

    # Fetch all documents using search with empty query
    offset = 0

    while offset < total_docs:
        print(f"\nFetching documents {offset} to {offset + batch_size}...")

        try:
            # Use search to get documents
            results = mq.index(index_name).search(
                q="",
                limit=batch_size,
                offset=offset,
                show_highlights=False
            )

            hits = results.get("hits", [])
            if not hits:
                break

            for doc in hits:
                updated_doc, was_cleaned, is_ref = process_document(doc)
                processed_count += 1

                if was_cleaned:
                    cleaned_count += 1
                    if not dry_run:
                        print(f"  Cleaned preamble from: {doc.get('name', 'unknown')} chunk {doc.get('chunk_num', '?')}")

                if is_ref:
                    reference_count += 1
                    if not dry_run:
                        print(f"  Marked as reference: {doc.get('name', 'unknown')} chunk {doc.get('chunk_num', '?')}")

                if not dry_run and (was_cleaned or is_ref):
                    updated_docs.append(updated_doc)

            # Batch update to Marqo
            if not dry_run and updated_docs and len(updated_docs) >= batch_size:
                print(f"  Updating {len(updated_docs)} documents in Marqo...")
                mq.index(index_name).add_documents(updated_docs)
                updated_docs = []

            offset += batch_size

        except Exception as e:
            print(f"Error processing batch at offset {offset}: {e}")
            break

    # Final batch update
    if not dry_run and updated_docs:
        print(f"\nUpdating final {len(updated_docs)} documents in Marqo...")
        mq.index(index_name).add_documents(updated_docs)

    # Summary
    print(f"\n{'='*50}")
    print(f"CLEANUP SUMMARY {'(DRY RUN)' if dry_run else ''}")
    print(f"{'='*50}")
    print(f"Total documents processed: {processed_count}")
    print(f"Documents with preamble cleaned: {cleaned_count}")
    print(f"Documents marked as references: {reference_count}")

    if dry_run:
        print("\nThis was a dry run. No changes were made.")
        print("Run without --dry-run to apply changes.")


def main():
    parser = argparse.ArgumentParser(description="Clean up Marqo index data")
    parser.add_argument("--marqo-url", default="http://localhost:8882", help="Marqo URL")
    parser.add_argument("--index-name", default="documents-index", help="Index name")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")

    args = parser.parse_args()

    run_cleanup(
        marqo_url=args.marqo_url,
        index_name=args.index_name,
        batch_size=args.batch_size,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()
