"""
Report SQLite child rows whose workflow_id has no documents row.

Read-only. Does not delete. Hard-delete with cascade=True (or a future GC
script with --apply) is the write path; this is the operator inventory.

Usage:
    python scripts/report_sqlite_orphans.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import db  # noqa: E402


def main() -> int:
    db.init_db()
    print(json.dumps(db.report_orphan_rows(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
