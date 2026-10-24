import json
from unittest.mock import patch

import pytest

from pipeline.domain_tags.base import (
    DomainTag,
    flatten_taxonomy_values,
    normalize_tag_key,
    parse_tag_list,
    split_query_and_tags,
    validate_tags_against_taxonomy,
)

# Marqo's tag ENCODING and filter grammar moved to the vector-store adapter; the
# taxonomy side (parsing, validation, the pure-text query split) stayed put.
from pipeline.vector_store import (
    build_domain_tags_filter as build_marqo_domain_tags_filter,
    merge_filter_strings as merge_marqo_filter_strings,
    tags_to_marqo_field,
)
from pipeline.domain_tags.gemma_tagger import _parse_tag_response
from tests.helpers import seed_document


def _tag(db, workflow_id, chunk_num, *keys, source="auto"):
    tags = [dict(zip(("dimension", "value"), key.split(":", 1))) for key in keys]
    db.replace_chunk_tags(workflow_id, chunk_num, tags, source=source)


def test_build_marqo_domain_tags_filter_and_merge():
    assert build_marqo_domain_tags_filter(["region:north", "REGION:north"]) == "domain_tags:(|region:north|)"
    assert (
        build_marqo_domain_tags_filter(["region:north", "topic:nutrition/feed"])
        == "domain_tags:(|region:north|) AND domain_tags:(|topic:nutrition/feed|)"
    )
    assert merge_marqo_filter_strings("is_reference:false", None) == "is_reference:false"
    assert (
        merge_marqo_filter_strings("is_reference:false", build_marqo_domain_tags_filter(["region:north"]))
        == "is_reference:false AND domain_tags:(|region:north|)"
    )


def test_split_query_and_tags_extracts_inline_domain_tags():
    assert split_query_and_tags("claim:eligibility") == ("", ["claim:eligibility"])
    assert split_query_and_tags("cow claim:eligibility topic:milk-production") == (
        "cow",
        ["claim:eligibility", "topic:milk-production"],
    )
    assert split_query_and_tags("topic:housing/management") == ("", ["topic:housing/management"])


def test_search_chunks_accepts_inline_domain_tags(db_connection):
    db = db_connection
    seed_document(db, "wf-search", stage="completed",
                  chunks=["Milking machine subsidy details", "Rubber mat housing guidance"])
    _tag(db, "wf-search", 1, "claim:eligibility", "topic:milk-production")
    _tag(db, "wf-search", 2, "topic:housing/management")

    chunks, total = db.search_chunks(query="claim:eligibility")
    assert total == 1
    assert chunks[0]["chunk_number"] == 1

    chunks, total = db.search_chunks(query="cow claim:eligibility")
    assert total == 0

    chunks, total = db.search_chunks(query="milking claim:eligibility")
    assert total == 1
    assert chunks[0]["chunk_number"] == 1


def test_parse_tag_list_deduplicates():
    tags = parse_tag_list(["region:north", "REGION:north", "topic:nutrition/feed"], source="manual")
    assert len(tags) == 2
    assert tags[0].key() == "region:north"
    assert tags[0].source == "manual"


@pytest.mark.parametrize(
    "tags, field",
    [
        ([DomainTag("topic", "nutrition/feed", "auto"), DomainTag("region", "north", "auto")],
         "|region:north|topic:nutrition/feed|"),
        ([], ""),
    ],
    ids=["sorted", "empty"],
)
def test_tags_to_marqo_field(tags, field):
    assert tags_to_marqo_field(tags) == field


def test_validate_tags_strict_filters_unknown():
    tags = [DomainTag("region", "north", "auto"), DomainTag("region", "not-a-real-union", "auto")]
    validated = validate_tags_against_taxonomy(tags, strict=True)
    assert len(validated) == 1
    assert validated[0].value == "north"


def test_parse_tag_response_json():
    allowed = {"region": {"north", "west"}, "topic": {"nutrition/feed"}}
    content = json.dumps({"tags": ["region:north", "topic:nutrition/feed", "region:fake", "anything:whatever"]})
    assert [t.key() for t in _parse_tag_response(content, allowed)] == ["region:north", "topic:nutrition/feed"]


def test_uppercase_taxonomy_values_are_matchable():
    assert "fmd" in flatten_taxonomy_values().get("condition", set())
    assert normalize_tag_key("condition:FMD") == "condition:fmd"
    tags = validate_tags_against_taxonomy([DomainTag("condition", "fmd", "auto")], strict=True)
    assert len(tags) == 1
    assert tags[0].key() == "condition:fmd"


def test_chunk_tags_db_roundtrip(db_connection):
    seed_document(db_connection, "wf-tags", stage="chunk_review", chunks=["Milking machine subsidy in north region"])
    _tag(db_connection, "wf-tags", 1, "region:north", "topic:housing/management")
    _tag(db_connection, "wf-tags", 1, "claim:benefit", source="manual")
    chunk = db_connection.get_chunk("wf-tags", 1)
    assert len(chunk["domain_tags"]) == 3
    assert "region:north" in chunk["domain_tags_flat"]
    assert "claim:benefit" in chunk["domain_tags_flat"]


def test_prepare_records_includes_domain_tags(db_connection):
    from pipeline.ingestion_records import prepare_records

    seed_document(db_connection, "wf-prep", document_id="doc-prep", filename="paripatra.pdf", stage="completed",
                  chunks=["Cattle insurance eligibility"])
    _tag(db_connection, "wf-prep", 1, "scheme:cattle-insurance", "claim:eligibility")
    records = prepare_records("doc-prep", "paripatra.pdf", db_connection.get_chunks("wf-prep", include_excluded=True))
    assert records[0]["domain_tags"] == "|claim:eligibility|scheme:cattle-insurance|"


@patch("pipeline.domain_tags.gemma_tagger.httpx.Client")
def test_gemma_tagger_suggest_tags(mock_client_cls):
    from pipeline.domain_tags.gemma_tagger import GemmaDomainTagger

    response = mock_client_cls.return_value.__enter__.return_value.post.return_value
    response.json.return_value = {
        "choices": [{"message": {"content": '{"tags": ["region:north", "topic:nutrition/feed"]}'}}]
    }

    tagger = GemmaDomainTagger(endpoint="http://localhost:8020/v1", model="gemma-4-31b-it")
    tags = tagger.suggest_tags("Feed rates for Gujarat", filename="feed.pdf")
    assert [t.key() for t in tags] == ["region:north", "topic:nutrition/feed"]
