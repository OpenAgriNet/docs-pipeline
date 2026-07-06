import json
from unittest.mock import MagicMock, patch

import pytest

from pipeline.domain_tags.base import (
    DomainTag,
    build_marqo_domain_tags_filter,
    merge_marqo_filter_strings,
    parse_tag_list,
    split_query_and_tags,
    tags_to_marqo_field,
    validate_tags_against_taxonomy,
)
from pipeline.domain_tags.gemma_tagger import _parse_tag_response


def test_build_marqo_domain_tags_filter_and_merge():
    assert build_marqo_domain_tags_filter(["region:sabar", "REGION:sabar"]) == "domain_tags:(region:sabar)"
    assert (
        build_marqo_domain_tags_filter(["region:sabar", "topic:nutrition/feed"])
        == "domain_tags:(region:sabar) AND domain_tags:(topic:nutrition/feed)"
    )
    assert merge_marqo_filter_strings("is_reference:false", None) == "is_reference:false"
    assert (
        merge_marqo_filter_strings(
            "is_reference:false",
            build_marqo_domain_tags_filter(["region:sabar"]),
        )
        == "is_reference:false AND domain_tags:(region:sabar)"
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
    db.upsert_document(
        workflow_id="wf-search",
        document_id="doc-search",
        filename="scheme.pdf",
        filepath="/tmp/scheme.pdf",
        stage="completed",
    )
    db.save_chunks(
        "wf-search",
        [
            {
                "chunk_number": 1,
                "original_text": "Milking machine subsidy details",
                "token_count": 10,
                "page_start": 1,
                "page_end": 1,
            },
            {
                "chunk_number": 2,
                "original_text": "Rubber mat housing guidance",
                "token_count": 10,
                "page_start": 2,
                "page_end": 2,
            },
        ],
    )
    db.replace_chunk_tags(
        "wf-search",
        1,
        [{"dimension": "claim", "value": "eligibility"}, {"dimension": "topic", "value": "milk-production"}],
        source="auto",
    )
    db.replace_chunk_tags(
        "wf-search",
        2,
        [{"dimension": "topic", "value": "housing/management"}],
        source="auto",
    )

    chunks, total = db.search_chunks(query="claim:eligibility")
    assert total == 1
    assert chunks[0]["chunk_number"] == 1

    chunks, total = db.search_chunks(query="cow claim:eligibility")
    assert total == 0

    chunks, total = db.search_chunks(query="milking claim:eligibility")
    assert total == 1
    assert chunks[0]["chunk_number"] == 1


def test_parse_tag_list_deduplicates():
    tags = parse_tag_list(["region:sabar", "REGION:sabar", "topic:nutrition/feed"], source="manual")
    assert len(tags) == 2
    assert tags[0].key() == "region:sabar"
    assert tags[0].source == "manual"


def test_tags_to_marqo_field_sorted():
    tags = [
        DomainTag("topic", "nutrition/feed", "auto"),
        DomainTag("region", "sabar", "auto"),
    ]
    assert tags_to_marqo_field(tags) == "region:sabar|topic:nutrition/feed"


def test_validate_tags_strict_filters_unknown():
    tags = [
        DomainTag("region", "sabar", "auto"),
        DomainTag("region", "not-a-real-union", "auto"),
    ]
    validated = validate_tags_against_taxonomy(tags, strict=True)
    assert len(validated) == 1
    assert validated[0].value == "sabar"


def test_parse_tag_response_json():
    allowed = {"region": {"sabar", "mehsana"}, "topic": {"nutrition/feed"}}
    content = json.dumps({"tags": ["region:sabar", "topic:nutrition/feed", "region:fake"]})
    tags = _parse_tag_response(content, allowed)
    assert [t.key() for t in tags] == ["region:sabar", "topic:nutrition/feed"]


def test_chunk_tags_db_roundtrip(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-tags",
        document_id="doc-tags",
        filename="test.pdf",
        filepath="/tmp/test.pdf",
        stage="chunk_review",
    )
    db.save_chunks(
        "wf-tags",
        [
            {
                "chunk_number": 1,
                "original_text": "Milking machine subsidy in Sabar union",
                "token_count": 10,
                "page_start": 1,
                "page_end": 1,
            }
        ],
    )
    db.replace_chunk_tags(
        "wf-tags",
        1,
        [{"dimension": "region", "value": "sabar"}, {"dimension": "topic", "value": "housing/management"}],
        source="auto",
    )
    db.replace_chunk_tags(
        "wf-tags",
        1,
        [{"dimension": "claim", "value": "benefit"}],
        source="manual",
    )
    chunk = db.get_chunk("wf-tags", 1)
    assert len(chunk["domain_tags"]) == 3
    assert "region:sabar" in chunk["domain_tags_flat"]
    assert "claim:benefit" in chunk["domain_tags_flat"]


def test_prepare_records_includes_domain_tags(db_connection):
    from pipeline.activities import _prepare_records

    db = db_connection
    db.upsert_document(
        workflow_id="wf-prep",
        document_id="doc-prep",
        filename="paripatra.pdf",
        filepath="/tmp/paripatra.pdf",
        stage="completed",
    )
    db.save_chunks(
        "wf-prep",
        [{"chunk_number": 1, "original_text": "Cattle insurance eligibility", "token_count": 5, "page_start": 1, "page_end": 1}],
    )
    db.replace_chunk_tags(
        "wf-prep",
        1,
        [{"dimension": "scheme", "value": "cattle-insurance"}, {"dimension": "claim", "value": "eligibility"}],
        source="auto",
    )
    chunks = db.get_chunks("wf-prep", include_excluded=True)
    records = _prepare_records("doc-prep", "paripatra.pdf", chunks)
    assert records[0]["domain_tags"] == "claim:eligibility|scheme:cattle-insurance"


@patch("pipeline.domain_tags.gemma_tagger.httpx.Client")
def test_gemma_tagger_suggest_tags(mock_client_cls):
    from pipeline.domain_tags.gemma_tagger import GemmaDomainTagger

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"tags": ["region:sabar", "topic:nutrition/feed"]}'}}]
    }
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value = mock_client

    tagger = GemmaDomainTagger(endpoint="http://localhost:8020/v1", model="gemma-4-31b-it")
    tags = tagger.suggest_tags("Feed rates for Gujarat", filename="feed.pdf")
    assert [t.key() for t in tags] == ["region:sabar", "topic:nutrition/feed"]
