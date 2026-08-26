"""
Purge MinIO objects listed in document_artifacts for one soft-deleted document.

Defaults to dry-run. Pass ``--apply`` to delete objects and stamp purged_at.

Usage:
    python scripts/purge_document_artifacts.py --workflow-id wf-abc
    python scripts/purge_document_artifacts.py --workflow-id wf-abc --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.services.artifacts import (  # noqa: E402
    ArtifactPurgeError,
    purge_document_artifacts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-id", required=True, help="Document workflow_id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete MinIO objects (default is dry-run)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = purge_document_artifacts(args.workflow_id, apply=args.apply)
    except ArtifactPurgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    if report["error_count"]:
        return 1
    if not args.apply:
        print("Dry-run only — re-run with --apply to delete MinIO objects.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
