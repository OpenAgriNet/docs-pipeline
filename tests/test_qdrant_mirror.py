import json
from types import SimpleNamespace

import pytest

from ops.qdrant_mirror import Mirror, decode_row, eligible_deletes, projected_fields
from pipeline.vector_store_qdrant import _payload_from_record, record_id_to_point_id


def test_metadata_projection_preserves_false_and_tensor_input():
    fields = projected_fields({"allFields": [
        {"name": "is_reference", "type": "bool", "features": ["filter"]},
        {"name": "text", "type": "text", "features": ["lexical_search"]},
        {"name": "text_for_embedding", "type": "text", "features": []},
    ]})
    assert decode_row({"marqo__id": "a", "marqo__filter_is_reference": 0,
                       "marqo__lexical_text": "milk", "text_for_embedding": "passage: milk"}, fields) == {
        "_id": "a", "is_reference": False, "text": "milk", "text_for_embedding": "passage: milk"}


def test_deletion_requires_two_observations_and_bounds():
    assert eligible_deletes(["a"], [], 10, 11, .1) == []
    assert eligible_deletes(["a"], ["a"], 10, 11, .1) == ["a"]
    with pytest.raises(RuntimeError, match="safety limit"):
        eligible_deletes(["a", "b"], ["a", "b"], 1, 3, .1)


class Client:
    def __init__(self, payloads):
        self.rows = {record_id_to_point_id(rid): payload.copy() for rid, payload in payloads.items()}
        self.writes = []

    def scroll(self, *args, **kwargs):
        return [SimpleNamespace(id=rid, payload=p) for rid, p in self.rows.items()], None

    def create_snapshot(self, collection, wait):
        self.writes.append(("snapshot", collection))
        return SimpleNamespace(name="before.snapshot")

    def overwrite_payload(self, collection, payload, points, wait):
        self.writes.append(("payload", points))
        for rid in points:
            self.rows[rid] = payload.copy()

    def delete(self, collection, points_selector, wait):
        self.writes.append(("delete", points_selector))
        for rid in points_selector:
            del self.rows[rid]

    def set_payload(self, collection, payload, points, wait):
        self.writes.append(("stage-deletion", points))
        for rid in points:
            self.rows[rid].update(payload)


def mirror(tmp_path, records):
    marker = tmp_path / "owner.json"
    marker.write_text(json.dumps({"collection": "owned"}))
    client = Client({rid: _payload_from_record(row) for rid, row in records.items()})
    config = {"state_dir": str(tmp_path / "state"), "target_collection": "owned", "tenant": "amul",
              "ownership_marker": str(marker), "source_index": "source", "source_settings_sha256": "unused"}
    instance = Mirror(config, client)
    from ops.qdrant_mirror import digest
    instance.config["source_settings_sha256"] = digest({})
    instance.api = lambda path: {}
    instance.scan = lambda settings: records.copy()
    return instance, client


def test_visibility_update_preserves_dense_vectors_and_scopes_target(tmp_path):
    source = {"a": {"_id": "a", "instance": "amul", "text": "milk", "query_enabled": True}}
    job, client = mirror(tmp_path, source)
    source["a"] = {**source["a"], "query_enabled": False}
    result = job.cycle()
    assert result["metadata_updates"] == 1
    assert result["upserts"] == 0
    assert client.rows[record_id_to_point_id("a")]["query_enabled"] is False
    assert client.writes == [("snapshot", "owned"), ("payload", [record_id_to_point_id("a")])]


def test_confirmed_removal_does_not_delete_other_records(tmp_path):
    source = {rid: {"_id": rid, "instance": "amul", "text": "milk"} for rid in ("a", "b")}
    job, client = mirror(tmp_path, source)
    del source["b"]
    first = job.cycle()
    assert first["pending_deletes"] == 1 and first["deletes"] == 0
    assert client.rows[record_id_to_point_id("b")]["query_enabled"] is False
    second = job.cycle()
    assert second["deletes"] == 1
    assert set(client.rows) == {record_id_to_point_id("a")}


def test_source_failure_never_becomes_empty_target(tmp_path):
    source = {"a": {"_id": "a", "instance": "amul", "text": "milk"}}
    job, client = mirror(tmp_path, source)
    def failed(settings):
        raise RuntimeError("source unavailable")
    job.scan = failed
    with pytest.raises(RuntimeError, match="unavailable"):
        job.cycle()
    assert not client.writes
    assert len(client.rows) == 1


def test_reappearing_record_is_unhidden_without_reembedding(tmp_path):
    source = {rid: {"_id": rid, "instance": "amul", "text": "milk"} for rid in ("a", "b")}
    job, client = mirror(tmp_path, source)
    removed = source.pop("b")
    job.cycle()
    source["b"] = removed
    result = job.cycle()
    assert result["metadata_updates"] == 1 and result["upserts"] == 0
    assert client.rows[record_id_to_point_id("b")] == _payload_from_record(removed)
    assert len(client.rows) == 2


def test_unowned_target_point_stops_before_writes(tmp_path):
    source = {"a": {"_id": "a", "instance": "amul", "text": "milk"}}
    job, client = mirror(tmp_path, source)
    client.rows[record_id_to_point_id("a")]["instance"] = "other"
    with pytest.raises(RuntimeError, match="namespace"):
        job.cycle()
    assert not client.writes


def test_model_or_schema_change_stops_before_scan(tmp_path):
    source = {"a": {"_id": "a", "instance": "amul", "text": "milk"}}
    job, client = mirror(tmp_path, source)
    job.api = lambda path: {"model": "different"}
    with pytest.raises(RuntimeError, match="settings changed"):
        job.cycle()
    assert not client.writes
