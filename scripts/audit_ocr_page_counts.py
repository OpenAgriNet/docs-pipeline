#!/usr/bin/env python3
"""Audit SQLite OCR page rows for internal consistency gaps."""

from __future__ import annotations

import sqlite3
import sys


def audit(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT d.workflow_id, d.filename, d.page_count, d.stage,
               COUNT(p.page_number) AS page_rows,
               MAX(p.page_number) AS max_page_number
        FROM documents d
        LEFT JOIN pages p ON p.workflow_id = d.workflow_id
        GROUP BY d.workflow_id
        ORDER BY d.filename
        """
    ).fetchall()

    mismatches = 0
    for row in rows:
        page_rows = int(row["page_rows"] or 0)
        max_page = int(row["max_page_number"] or 0)
        page_count = int(row["page_count"] or 0)
        issues = []
        if page_count and page_count != page_rows:
            issues.append(f"page_count={page_count} != page_rows={page_rows}")
        if max_page and max_page != page_rows:
            issues.append(f"max_page_number={max_page} != page_rows={page_rows}")
        if page_rows and max_page and list(range(1, max_page + 1)) != sorted(
            n
            for (n,) in conn.execute(
                "SELECT page_number FROM pages WHERE workflow_id = ? ORDER BY page_number",
                (row["workflow_id"],),
            ).fetchall()
        ):
            issues.append("non-contiguous page_number sequence")
        if issues:
            mismatches += 1
            print(f"ISSUE {row['filename']} [{row['stage']}]: " + "; ".join(issues))

    print(f"documents={len(rows)} issues={mismatches}")
    return mismatches


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: audit_ocr_page_counts.py <documents.db>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(audit(sys.argv[1]))


if __name__ == "__main__":
    main()
