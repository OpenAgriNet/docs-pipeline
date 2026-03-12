"""
Configuration and environment validation for the Document Ingestion Pipeline.

Validates required environment variables and provides typed configuration access.
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """Application configuration with validated environment variables."""

    # Required
    minio_access_key: str
    minio_secret_key: str
    mistral_api_key: str

    # Optional with defaults
    temporal_host: str = "localhost:7233"
    minio_endpoint: str = "localhost:9000"
    minio_bucket: str = "documents"
    marqo_url: str = "http://localhost:8882"
    document_db_path: str = "/data/documents.db"
    lang_detect_url: str = "http://lang-detect:3001"
    translation_provider: str = "mistral"
    translation_model: str = "mistral-large-latest"

    # CORS
    cors_origins: list[str] = None

    # Rate limiting
    rate_limit_default: str = "100/minute"
    rate_limit_upload: str = "10/minute"

    # File access
    allowed_file_paths: list[str] = None

    def __post_init__(self):
        if self.cors_origins is None:
            self.cors_origins = ["https://localhost:3000", "http://localhost:3000"]
        if self.allowed_file_paths is None:
            self.allowed_file_paths = ["/app/books", "/data/documents"]


class ConfigurationError(Exception):
    """Raised when required configuration is missing."""
    pass


def validate_environment() -> list[str]:
    """
    Validate that all required environment variables are set.

    Returns list of missing/invalid variables.
    """
    errors = []

    # Required variables
    required = [
        ("MINIO_ACCESS_KEY", "MinIO access key for object storage"),
        ("MINIO_SECRET_KEY", "MinIO secret key for object storage"),
        ("MISTRAL_API_KEY", "Mistral API key for OCR processing"),
    ]

    for var_name, description in required:
        if not os.environ.get(var_name):
            errors.append(f"{var_name}: {description}")

    return errors


def load_config() -> Config:
    """
    Load and validate configuration from environment.

    Raises ConfigurationError if required variables are missing.
    """
    errors = validate_environment()
    if errors:
        error_msg = "Missing required environment variables:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ConfigurationError(error_msg)

    return Config(
        # Required
        minio_access_key=os.environ["MINIO_ACCESS_KEY"],
        minio_secret_key=os.environ["MINIO_SECRET_KEY"],
        mistral_api_key=os.environ["MISTRAL_API_KEY"],

        # Optional
        temporal_host=os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        minio_endpoint=os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        minio_bucket=os.environ.get("MINIO_BUCKET", "documents"),
        marqo_url=os.environ.get("MARQO_URL", "http://localhost:8882"),
        document_db_path=os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db"),
        lang_detect_url=os.environ.get("LANG_DETECT_URL", "http://lang-detect:3001"),
        translation_provider=os.environ.get("TRANSLATION_PROVIDER", "mistral"),
        translation_model=os.environ.get("TRANSLATION_MODEL", "mistral-large-latest"),

        # CORS
        cors_origins=os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") else None,

        # Rate limiting
        rate_limit_default=os.environ.get("RATE_LIMIT_DEFAULT", "100/minute"),
        rate_limit_upload=os.environ.get("RATE_LIMIT_UPLOAD", "10/minute"),

        # File access
        allowed_file_paths=os.environ.get("ALLOWED_FILE_PATHS", "").split(",") if os.environ.get("ALLOWED_FILE_PATHS") else None,
    )


def print_config_status():
    """Print configuration status for debugging."""
    print("=" * 60)
    print("Configuration Status")
    print("=" * 60)

    errors = validate_environment()
    if errors:
        print("\n❌ Missing required variables:")
        for error in errors:
            print(f"   - {error}")
    else:
        print("\n✓ All required variables present")

    print("\n📋 Current configuration:")
    optional = [
        ("TEMPORAL_HOST", "localhost:7233"),
        ("MINIO_ENDPOINT", "localhost:9000"),
        ("MINIO_BUCKET", "documents"),
        ("MARQO_URL", "http://localhost:8882"),
        ("DOCUMENT_DB_PATH", "/data/documents.db"),
        ("LANG_DETECT_URL", "http://lang-detect:3001"),
        ("TRANSLATION_PROVIDER", "mistral"),
        ("TRANSLATION_MODEL", "mistral-large-latest"),
        ("CORS_ORIGINS", "(default)"),
        ("RATE_LIMIT_DEFAULT", "100/minute"),
        ("RATE_LIMIT_UPLOAD", "10/minute"),
    ]

    for var_name, default in optional:
        value = os.environ.get(var_name, default)
        print(f"   {var_name}: {value}")

    print("=" * 60)


if __name__ == "__main__":
    print_config_status()
