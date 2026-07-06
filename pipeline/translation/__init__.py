"""Pluggable translation providers for the docs pipeline."""

from .base import TranslationConfig, TranslationProvider
from .service import (
    clean_translation,
    get_translation_provider,
    load_translation_config,
    translate_pages,
)

__all__ = [
    "TranslationConfig",
    "TranslationProvider",
    "clean_translation",
    "get_translation_provider",
    "load_translation_config",
    "translate_pages",
]
