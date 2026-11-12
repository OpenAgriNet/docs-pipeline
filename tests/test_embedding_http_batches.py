from unittest.mock import Mock
import pytest
from pipeline.embedding import HttpDenseFastEmbedSparse


def embedder():
    e = object.__new__(HttpDenseFastEmbedSparse)
    e.api_key = 'test-only'; e.model = 'e5'; e._url = 'http://gpu/v1/embeddings'
    e.timeout_seconds = 120.; e.normalize = False
    return e


def test_many_sentence_windows_use_bounded_ordered_requests(monkeypatch):
    calls = []
    def post(url, *, headers, json):
        calls.append(json['input'])
        data = [{'index': i, 'embedding': [float(text.split(':')[1]), 2.]}
                for i, text in enumerate(json['input'])]
        return Mock(json=lambda: {'data': list(reversed(data))})
    client = Mock(); client.__enter__ = Mock(return_value=client); client.__exit__ = Mock(return_value=False)
    client.post = post
    monkeypatch.setattr('pipeline.embedding.httpx.Client', lambda **kwargs: client)
    vectors = embedder()._embed_dense([f'passage:{i}' for i in range(300)])
    assert [len(x) for x in calls] == [64, 64, 64, 64, 44]
    assert vectors == [[float(i), 2.] for i in range(300)]


def test_duplicate_response_indices_fail_before_upsert(monkeypatch):
    client = Mock(); client.__enter__ = Mock(return_value=client); client.__exit__ = Mock(return_value=False)
    client.post.return_value.json.return_value = {'data': [
        {'index': 0, 'embedding': [1.]}, {'index': 0, 'embedding': [2.]}]}
    monkeypatch.setattr('pipeline.embedding.httpx.Client', lambda **kwargs: client)
    with pytest.raises(RuntimeError, match='missing or duplicate'):
        embedder()._embed_dense(['a', 'b'])
