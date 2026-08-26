"""Direct tests for ``pipeline.services.search`` (framework-free boundary)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipeline.services import search as search_service
from pipeline.services.search import SearchServiceError, empty_search_result, run_search
from pipeline.vector_store import VectorStoreError


@pytest.mark.unit
def test_search_service_does_not_import_fastapi():
    """Pilot boundary: services must not pull FastAPI into the domain layer."""
    assert "fastapi" not in search_service.__dict__
    assert not hasattr(search_service, "HTTPException")
    import inspect

    source = inspect.getsource(search_service)
    assert "fastapi" not in source
    assert "HTTPException" not in source


@pytest.mark.unit
def test_prepare_query_for_e5_prefixes_once():
    assert search_service.prepare_query_for_e5("milk yield") == "query: milk yield"
    assert search_service.prepare_query_for_e5("query: already") == "query: already"


@pytest.mark.unit
def test_expand_query_gu_v1_appends_terms():
    expanded = search_service.expand_query("fever", "gu-v1")
    assert expanded.startswith("fever")
    assert "pyrexia" in expanded


@pytest.mark.unit
def test_empty_search_result_shape():
    result = empty_search_result("milk", include_raw_hits=True)
    assert result["effective_config"]["index_name"] is None
    assert result["hits"] == []
    assert result["raw_hits"] == []
    assert result["candidate_count"] == 0
    assert result["final_count"] == 0


@pytest.mark.unit
def test_run_search_happy_path_calls_store_and_caps_per_doc():
    store = MagicMock()
    store.search.return_value = {
        "hits": [
            {"_id": "1", "doc_id": "d1", "text": "a", "_score": 3.0},
            {"_id": "2", "doc_id": "d1", "text": "b", "_score": 2.0},
            {"_id": "3", "doc_id": "d1", "text": "c", "_score": 1.0},
            {"_id": "4", "doc_id": "d2", "text": "d", "_score": 0.5},
        ]
    }
    result = run_search(
        index_name="documents-index",
        query="milk",
        settings={"maxChunksPerDoc": 2, "limit": 10, "useE5Prefix": True},
        payload={},
        store=store,
    )
    store.search.assert_called_once()
    call_args = store.search.call_args
    assert call_args.args[0] == "documents-index"
    assert call_args.kwargs["q"].startswith("query:")
    assert result["final_count"] == 3  # 2 from d1 + 1 from d2
    assert [h["doc_id"] for h in result["hits"]] == ["d1", "d1", "d2"]
    assert result["candidate_count"] == 4


@pytest.mark.unit
def test_run_search_marqo_failure_raises_search_service_error():
    store = MagicMock()
    store.search.side_effect = VectorStoreError("down")
    with pytest.raises(SearchServiceError, match="Marqo search failed"):
        run_search(
            index_name="documents-index",
            query="milk",
            settings={},
            payload={},
            store=store,
        )


@pytest.mark.unit
def test_run_search_unsupported_domain_tags_raises_search_service_error():
    store = MagicMock()
    store.field_names.return_value = {"text", "description"}
    with pytest.raises(SearchServiceError, match="does not support domain tag filters"):
        run_search(
            index_name="legacy-index",
            query="milk",
            settings={},
            payload={"domain_tags": ["species:cattle"]},
            store=store,
        )
    store.search.assert_not_called()
