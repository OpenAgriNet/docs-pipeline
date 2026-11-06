"""Advisory location fields are indexed on new collections and added to existing ones."""

from pipeline.vector_store.qdrant_store import LOCATION_PAYLOAD_INDEXES, QdrantVectorStore


class FakeClient:
    def __init__(self, exists):
        self.exists = exists
        self.indexed = []
        self.created = []

    def get_collection(self, name):
        if not self.exists:
            raise RuntimeError("not found")
        return object()

    def create_collection(self, **kwargs):
        self.created.append(kwargs["collection_name"])

    def delete_collection(self, name):
        self.exists = False

    def create_payload_index(self, collection_name, field_name, field_schema):
        self.indexed.append(field_name)


def test_existing_collection_gets_only_the_location_indexes():
    client = FakeClient(exists=True)
    result = QdrantVectorStore(client=client).ensure_collection("docs")
    assert result["created"] is False
    assert client.created == []
    assert sorted(client.indexed) == sorted(LOCATION_PAYLOAD_INDEXES)


def test_new_collection_gets_the_original_and_the_location_indexes():
    client = FakeClient(exists=False)
    result = QdrantVectorStore(client=client).ensure_collection("docs")
    assert result["created"] is True
    assert set(LOCATION_PAYLOAD_INDEXES) <= set(client.indexed)
    assert {"doc_id", "type", "source"} <= set(client.indexed)


def test_a_failing_index_call_does_not_break_ensure_collection():
    client = FakeClient(exists=True)

    def boom(**_):
        raise RuntimeError("already exists")

    client.create_payload_index = lambda collection_name, field_name, field_schema: boom()
    assert QdrantVectorStore(client=client).ensure_collection("docs")["created"] is False
