"""Domain tagging service configuration and provider selection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .base import load_taxonomy
from .gemma_tagger import GemmaDomainTagger

PROVIDERS = {
    "gemma_vllm": GemmaDomainTagger,
    "gemma": GemmaDomainTagger,
    "gemma4": GemmaDomainTagger,
}


@dataclass
class DomainTaggingConfig:
    enabled: bool = True
    provider: str = "gemma_vllm"
    model: str = "gemma-4-31b-it"
    endpoint: str = ""
    api_key: str = ""
    request_timeout_seconds: float = 120.0
    max_output_tokens: int = 1024
    strict_taxonomy: bool = True


def load_domain_tagging_config() -> DomainTaggingConfig:
    enabled = os.environ.get("DOMAIN_TAGGING_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }
    provider = os.environ.get("DOMAIN_TAGGING_PROVIDER", "gemma_vllm").strip().lower()
    model = (
        os.environ.get("DOMAIN_TAGGING_MODEL")
        or os.environ.get("AGRINET_GEMMA_MODEL_NAME")
        or os.environ.get("TRANSLATION_MODEL")
        or "google/gemma-4-31b-it"
    ).strip()
    endpoint = (
        os.environ.get("DOMAIN_TAGGING_VLLM_BASE_URL", "").strip()
        or os.environ.get("AGRINET_GEMMA_BASE_URL", "").strip()
        or os.environ.get("TRANSLATION_VLLM_BASE_URL", "").strip()
        or "http://localhost:8020/v1"
    )
    if endpoint and not endpoint.rstrip("/").endswith("/v1"):
        endpoint = endpoint.rstrip("/") + "/v1"
    api_key = (
        os.environ.get("DOMAIN_TAGGING_API_KEY")
        or os.environ.get("TRANSLATION_API_KEY")
        or os.environ.get("AGRINET_GEMMA_API_KEY")
        or ""
    ).strip()
    strict = os.environ.get("DOMAIN_TAGGING_STRICT_TAXONOMY", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }
    return DomainTaggingConfig(
        enabled=enabled,
        provider=provider,
        model=model or "google/gemma-4-31b-it",
        endpoint=endpoint,
        api_key=api_key,
        request_timeout_seconds=float(os.environ.get("DOMAIN_TAGGING_REQUEST_TIMEOUT_SECONDS", "120")),
        max_output_tokens=int(os.environ.get("DOMAIN_TAGGING_MAX_OUTPUT_TOKENS", "1024")),
        strict_taxonomy=strict,
    )


def get_domain_tagger(config: Optional[DomainTaggingConfig] = None) -> GemmaDomainTagger:
    config = config or load_domain_tagging_config()
    provider_cls = PROVIDERS.get(config.provider)
    if provider_cls is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported domain tagging provider '{config.provider}'. Supported: {supported}")
    return provider_cls(
        endpoint=config.endpoint,
        model=config.model,
        api_key=config.api_key,
        request_timeout_seconds=config.request_timeout_seconds,
        max_output_tokens=config.max_output_tokens,
    )


def get_taxonomy_for_api() -> dict:
    return load_taxonomy()
