"""Tests for the embedding token counter used to size advisory chunks."""

from types import SimpleNamespace

from pipeline.vector_store import embeddings


def test_count_tokens_local_uses_tokenizer(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "sentence_transformers")
    fake = SimpleNamespace(tokenizer=SimpleNamespace(encode=lambda text: text.split()))
    monkeypatch.setattr(embeddings, "_get_local_embedder", lambda: fake)
    # "passage: a b c" -> passage:, a, b, c
    assert embeddings.count_tokens("a b c") == 4


def test_count_tokens_remote_estimates_from_length(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    text = "x" * 400
    assert embeddings.count_tokens(text) == len("passage: " + text) // 4


def test_count_tokens_remote_is_at_least_one(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    assert embeddings.count_tokens("") >= 1
