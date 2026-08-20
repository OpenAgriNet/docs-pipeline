"""
Configuration and environment validation for the document ingestion pipeline.

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

    # Optional with defaults
    temporal_host: str = "localhost:7233"
    minio_endpoint: str = "localhost:9000"
    minio_bucket: str = "documents"
    marqo_url: str = "http://localhost:8882"
    document_db_path: str = "/data/documents.db"
    lang_detect_url: str = "http://lang-detect:3001"
    translation_provider: str = "gemma_vllm"
    translation_model: str = "gemma-4-31b-it"
    translation_vllm_base_url: str = "http://localhost:8020/v1"
    ocr_provider: str = "chandra"
    ocr_model: str = "chandra"
    chandra_vllm_base_url: str = ""
    chandra_max_output_tokens: int = 12288
    chandra_ocr_max_workers: int = 4
    translation_page_concurrency: int = 1
    translation_max_retries: int = 6
    translation_retry_base_seconds: float = 2.0
    chunking_provider: str = "recursive_splitter"
    chunking_model: str = "recursive_splitter"
    chunking_target_chunk_tokens: int = 450
    chunking_max_chunk_tokens: int = 450
    chunking_min_chunk_tokens: int = 100
    chunking_overlap_tokens: int = 128
    chunking_max_pages_per_chunk: int = 8
    chunking_page_window_size: int = 8
    temporal_max_concurrent_activities: int = 4

    # CORS
    cors_origins: list[str] = None

    # Rate limiting
    rate_limit_default: str = "100/minute"
    rate_limit_upload: str = "10/minute"

    # File access
    allowed_file_paths: list[str] = None

    # Master Catalog (Postgres) — code/name/tool_name/prompt_snippet, generic
    # across content types (scheme, advisory, ...), not scheme-specific
    master_catalog_pg_host: str = "localhost"
    master_catalog_pg_port: int = 5432
    master_catalog_pg_db: str = "master_catalog"
    master_catalog_pg_user: str = "master_catalog"
    master_catalog_pg_password: str = ""
    master_catalog_pg_sslmode: str = "disable"

    # AI layer Redis (bharat-oan-api) — direct write of the catalog snapshot
    ai_layer_redis_host: str = ""
    ai_layer_redis_port: int = 6379
    ai_layer_redis_db: int = 0
    ai_layer_redis_password: str = ""
    master_catalog_redis_ttl_seconds: int = 172800

    def __post_init__(self):
        if self.cors_origins is None:
            self.cors_origins = ["http://localhost:3000"]
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

        # Optional
        temporal_host=os.environ.get("TEMPORAL_HOST", "localhost:7233"),
        minio_endpoint=os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        minio_bucket=os.environ.get("MINIO_BUCKET", "documents"),
        marqo_url=os.environ.get("MARQO_URL", "http://localhost:8882"),
        document_db_path=os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db"),
        lang_detect_url=os.environ.get("LANG_DETECT_URL", "http://lang-detect:3001"),
        translation_provider=os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm"),
        translation_model=os.environ.get("TRANSLATION_MODEL", "gemma-4-31b-it"),
        translation_vllm_base_url=os.environ.get("TRANSLATION_VLLM_BASE_URL", "http://localhost:8020/v1"),
        ocr_provider=os.environ.get("OCR_PROVIDER", "chandra"),
        ocr_model=os.environ.get("OCR_MODEL", "chandra"),
        chandra_vllm_base_url=os.environ.get("CHANDRA_VLLM_BASE_URL", ""),
        chandra_max_output_tokens=int(os.environ.get("CHANDRA_MAX_OUTPUT_TOKENS", "12288")),
        chandra_ocr_max_workers=int(os.environ.get("CHANDRA_OCR_MAX_WORKERS", "4")),
        translation_page_concurrency=int(os.environ.get("TRANSLATION_PAGE_CONCURRENCY", "1")),
        translation_max_retries=int(os.environ.get("TRANSLATION_MAX_RETRIES", "6")),
        translation_retry_base_seconds=float(os.environ.get("TRANSLATION_RETRY_BASE_SECONDS", "2.0")),
        chunking_provider=os.environ.get("CHUNKING_PROVIDER", "recursive_splitter"),
        chunking_model=os.environ.get("CHUNKING_MODEL", "recursive_splitter"),
        chunking_target_chunk_tokens=int(os.environ.get("CHUNKING_TARGET_CHUNK_TOKENS", "450")),
        chunking_max_chunk_tokens=int(os.environ.get("CHUNKING_MAX_CHUNK_TOKENS", "450")),
        chunking_min_chunk_tokens=int(os.environ.get("CHUNKING_MIN_CHUNK_TOKENS", "100")),
        chunking_overlap_tokens=int(os.environ.get("CHUNKING_OVERLAP_TOKENS", "128")),
        chunking_max_pages_per_chunk=int(os.environ.get("CHUNKING_MAX_PAGES_PER_CHUNK", "8")),
        chunking_page_window_size=int(os.environ.get("CHUNKING_PAGE_WINDOW_SIZE", "8")),
        temporal_max_concurrent_activities=int(os.environ.get("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "4")),

        # CORS
        cors_origins=os.environ.get("CORS_ORIGINS", "").split(",") if os.environ.get("CORS_ORIGINS") else None,

        # Rate limiting
        rate_limit_default=os.environ.get("RATE_LIMIT_DEFAULT", "100/minute"),
        rate_limit_upload=os.environ.get("RATE_LIMIT_UPLOAD", "10/minute"),

        # File access
        allowed_file_paths=os.environ.get("ALLOWED_FILE_PATHS", "").split(",") if os.environ.get("ALLOWED_FILE_PATHS") else None,

        # Master Catalog (Postgres)
        master_catalog_pg_host=os.environ.get("MASTER_CATALOG_PG_HOST", "localhost"),
        master_catalog_pg_port=int(os.environ.get("MASTER_CATALOG_PG_PORT", "5432")),
        master_catalog_pg_db=os.environ.get("MASTER_CATALOG_PG_DB", "master_catalog"),
        master_catalog_pg_user=os.environ.get("MASTER_CATALOG_PG_USER", "master_catalog"),
        master_catalog_pg_password=os.environ.get("MASTER_CATALOG_PG_PASSWORD", ""),
        master_catalog_pg_sslmode=os.environ.get("MASTER_CATALOG_PG_SSLMODE", "disable"),

        # AI layer Redis
        ai_layer_redis_host=os.environ.get("AI_LAYER_REDIS_HOST", ""),
        ai_layer_redis_port=int(os.environ.get("AI_LAYER_REDIS_PORT", "6379")),
        ai_layer_redis_db=int(os.environ.get("AI_LAYER_REDIS_DB", "0")),
        ai_layer_redis_password=os.environ.get("AI_LAYER_REDIS_PASSWORD", ""),
        master_catalog_redis_ttl_seconds=int(os.environ.get("MASTER_CATALOG_REDIS_TTL_SECONDS", "172800")),
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
        ("TRANSLATION_PROVIDER", "gemma_vllm"),
        ("TRANSLATION_MODEL", "gemma-4"),
        ("TRANSLATION_VLLM_BASE_URL", "http://localhost:8020/v1"),
        ("OCR_PROVIDER", "chandra"),
        ("OCR_MODEL", "chandra"),
        ("CHANDRA_VLLM_BASE_URL", ""),
        ("CHANDRA_MAX_OUTPUT_TOKENS", "12288"),
        ("CHANDRA_OCR_MAX_WORKERS", "4"),
        ("TRANSLATION_PAGE_CONCURRENCY", "1"),
        ("TRANSLATION_MAX_RETRIES", "6"),
        ("TRANSLATION_RETRY_BASE_SECONDS", "2.0"),
        ("CHUNKING_PROVIDER", "recursive_splitter"),
        ("CHUNKING_MODEL", "recursive_splitter"),
        ("CHUNKING_TARGET_CHUNK_TOKENS", "450"),
        ("CHUNKING_MAX_CHUNK_TOKENS", "450"),
        ("CHUNKING_MIN_CHUNK_TOKENS", "100"),
        ("CHUNKING_OVERLAP_TOKENS", "128"),
        ("CHUNKING_MAX_PAGES_PER_CHUNK", "8"),
        ("CHUNKING_PAGE_WINDOW_SIZE", "8"),
        ("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "4"),
        ("CORS_ORIGINS", "(default)"),
        ("RATE_LIMIT_DEFAULT", "100/minute"),
        ("RATE_LIMIT_UPLOAD", "10/minute"),
        ("MASTER_CATALOG_PG_HOST", "localhost"),
        ("MASTER_CATALOG_PG_DB", "master_catalog"),
        ("AI_LAYER_REDIS_HOST", "(unset — Redis push disabled)"),
    ]

    for var_name, default in optional:
        value = os.environ.get(var_name, default)
        print(f"   {var_name}: {value}")

    print("=" * 60)


if __name__ == "__main__":
    print_config_status()
