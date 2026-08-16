"""Source-file validation and identity helpers."""

import hashlib
import os
from pathlib import Path

from fastapi import HTTPException


ALLOWED_FILE_PATHS = os.environ.get(
    "ALLOWED_FILE_PATHS", "/app/books,/data/documents"
).split(",")
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff",
}


def validate_file_path(filepath: str) -> str:
    """Validate that a source path is local-and-allowed or a supported MinIO URI."""
    if filepath.startswith("minio://"):
        suffix = Path(filepath).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported file type: {suffix}")
        return filepath

    path = Path(filepath).resolve()
    for allowed_base in ALLOWED_FILE_PATHS:
        allowed_path = Path(allowed_base.strip()).resolve()
        try:
            path.relative_to(allowed_path)
            if not path.exists():
                raise HTTPException(404, "File not found")
            if not path.is_file():
                raise HTTPException(400, "Path is not a file")
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                raise HTTPException(400, f"Unsupported file type: {path.suffix.lower()}")
            return str(path)
        except ValueError:
            continue

    raise HTTPException(403, "Access to this file path is not allowed")


def get_filename_from_path(filepath: str) -> str:
    """Extract a filename from a local path or ``minio://`` URI."""
    if filepath.startswith("minio://"):
        return filepath.split("/")[-1]
    return Path(filepath).name


def compute_file_fingerprint(filepath: Path) -> str:
    """Return the MD5 fingerprint used as the canonical source identifier."""
    md5 = hashlib.md5()
    with filepath.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()
