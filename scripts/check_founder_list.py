#!/usr/bin/env python3
"""Check founder/Kanav document list against Marqo + SQLite (fuzzy, not exact-only)."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

import marqo  # noqa: E402

MARQO_URL = os.environ.get("MARQO_URL", "http://marqo:8882")
MARQO_INDEX = os.environ.get("MARQO_INDEX", "amul-veterinary-index")
DB_PATH = os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db")

# Kanav list from spreadsheet (unhighlighted + highlighted)
FOUNDER_FILES = [
    "Amul_Homeopathic_Brochure_Gujarati (2) (1).pdf",
    "SABAR PARIPATRA NO.52.pdf",
    "SABAR TEAT DIP CIRCULAR NO.69.pdf",
    "SABAR PREGNANCY TEST KIT CIRCULAR NO.73.pdf",
    "SABAR CATTLE INSURANCE PARIPATRA NO. 73.pdf",
    "SABAR PARIPATRA NO. 5.pdf",
    "SABAR Paripatra No.66 (2).docx",
    "SABAR 70. Milking Machine Subsidy Circular (1).pdf",
    "SABAR Paripatra No.29.pdf",
    "SABAR SILAGE PARIPATRA NO.6.pdf",
    "cattle_feed_rate_change_paripatra2.pdf",
    "Cattle feed Rate change Paripatra-2",
]


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def collect_marqo_catalog(index) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Return doc_id -> {filenames}, normalized_stem -> best filename."""
    doc_filenames: dict[str, set[str]] = {}
    norm_to_filename: dict[str, str] = {}
    seen_doc_ids: set[str] = set()
    filter_string: str | None = None
    batch = 1000

    while True:
        offset = 0
        round_chunks = 0
        while True:
            limit = min(batch, 10000 - offset, 1000)
            if limit <= 0:
                break
            kwargs = {
                "q": "",
                "limit": limit,
                "offset": offset,
                "attributes_to_retrieve": ["filename", "doc_id", "name_en", "name_gu"],
            }
            if filter_string:
                kwargs["filter_string"] = filter_string
            hits = index.search(**kwargs).get("hits", [])
            if not hits:
                break
            for hit in hits:
                did = hit.get("doc_id")
                fn = (hit.get("filename") or "").strip()
                if did:
                    seen_doc_ids.add(did)
                    doc_filenames.setdefault(did, set())
                    if fn:
                        doc_filenames[did].add(fn)
                        norm_to_filename.setdefault(normalize_key(fn), fn)
                        norm_to_filename.setdefault(normalize_key(Path(fn).stem), fn)
                for field in ("name_en", "name_gu"):
                    val = (hit.get(field) or "").strip()
                    if val:
                        norm_to_filename.setdefault(normalize_key(val), fn or val)
            round_chunks += len(hits)
            if len(hits) < limit:
                break
            offset += len(hits)
            if offset >= 10000:
                break
        if round_chunks == 0:
            break
        clause = " OR ".join(f"doc_id:{d}" for d in sorted(seen_doc_ids))
        filter_string = f"NOT ({clause})"

    return doc_filenames, norm_to_filename


def sqlite_lookup(conn: sqlite3.Connection, name: str) -> list[tuple]:
    like = f"%{name[:30]}%"
    return conn.execute(
        """
        SELECT filename, stage, chunk_count, workflow_id
        FROM documents
        WHERE lower(filename) LIKE lower(?)
           OR lower(filename) LIKE lower(?)
        ORDER BY updated_at DESC
        LIMIT 5
        """,
        (like, f"%{Path(name).stem[:20]}%"),
    ).fetchall()


def marqo_keyword_hits(index, keyword: str, limit: int = 5) -> list[dict]:
    try:
        return index.search(
            q=keyword,
            limit=limit,
            attributes_to_retrieve=["filename", "doc_id", "name_en", "text"],
        ).get("hits", [])
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--keywords", nargs="*", default=["SABAR", "PARIPATRA", "Homeopathic", "cattle feed"])
    args = parser.parse_args()

    mq = marqo.Client(url=MARQO_URL)
    index = mq.index(MARQO_INDEX)
    stats = index.get_stats()
    print(f"Marqo index: {MARQO_INDEX}")
    print(f"Marqo total chunks: {stats.get('numberOfDocuments')}")
    print("Scanning Marqo for unique documents (by doc_id)...")

    doc_filenames, norm_catalog = collect_marqo_catalog(index)
    unique_docs = len(doc_filenames)
    unique_filenames = sorted({fn for names in doc_filenames.values() for fn in names})
    print(f"Unique documents (doc_id) in Marqo: {unique_docs}")
    print(f"Unique filenames in Marqo: {len(unique_filenames)}")
    print()

    conn = sqlite3.connect(args.db)
    sqlite_completed = conn.execute(
        "SELECT count(*) FROM documents WHERE stage='completed'"
    ).fetchone()[0]
    sqlite_total = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    manifest_rows = conn.execute(
        "SELECT count(*) FROM document_manifest_entries"
    ).fetchone()[0]
    print(f"SQLite documents total: {sqlite_total}, completed: {sqlite_completed}")
    print(f"Manifest entries in SQLite: {manifest_rows}")
    print()

    print("=== Founder list (fuzzy match against Marqo catalog) ===")
    print(f"{'QUERY':<55} {'MARQO':<10} MATCHED AS")
    print("-" * 100)
    for name in FOUNDER_FILES:
        key = normalize_key(name)
        stem_key = normalize_key(Path(name).stem)
        matched = norm_catalog.get(key) or norm_catalog.get(stem_key)
        if matched:
            print(f"{name:<55} {'INGESTED':<10} {matched}")
            continue
        # partial: any catalog key containing significant token
        tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 5]
        partial = None
        for token in tokens:
            for nk, fn in norm_catalog.items():
                if token in nk:
                    partial = fn
                    break
            if partial:
                break
        if partial:
            print(f"{name:<55} {'LIKELY':<10} {partial}")
        else:
            print(f"{name:<55} {'MISSING':<10} -")

    print()
    print("=== SQLite partial matches for founder list ===")
    for name in FOUNDER_FILES:
        rows = sqlite_lookup(conn, name)
        if rows:
            print(f"{name} -> {rows[0]}")
        else:
            print(f"{name} -> not in SQLite")

    print()
    print("=== Marqo keyword search (lexical/semantic) ===")
    for kw in args.keywords:
        hits = marqo_keyword_hits(index, kw, limit=8)
        fns = sorted({h.get("filename", "") for h in hits if h.get("filename")})
        print(f"\nkeyword '{kw}': {len(hits)} hits, filenames: {fns[:8]}")

    print()
    print("=== All Marqo filenames containing 'SABAR' or 'sabar' ===")
    sabar = [f for f in unique_filenames if "sabar" in f.lower()]
    print(f"count: {len(sabar)}")
    for f in sabar[:30]:
        print(f"  {f}")
    if len(sabar) > 30:
        print(f"  ... and {len(sabar) - 30} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
