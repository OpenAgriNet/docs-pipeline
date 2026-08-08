"""Domain tag taxonomy and auto/manual tagging."""

from ..vector_store import tags_to_marqo_field  # moved: Marqo's encoding, re-exported here
from .base import DomainTag, load_taxonomy, parse_tag_list
from .service import get_domain_tagger, get_taxonomy_for_api, load_domain_tagging_config

__all__ = [
    "DomainTag",
    "load_taxonomy",
    "parse_tag_list",
    "tags_to_marqo_field",
    "get_domain_tagger",
    "get_taxonomy_for_api",
    "load_domain_tagging_config",
]
