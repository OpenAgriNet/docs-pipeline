"""Opt-in live lifecycle check using newly created, isolated source/target names."""
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from ops.qdrant_mirror import Mirror, digest, write_json
from pipeline.vector_store_qdrant import QdrantStore, record_id_to_point_id


def run(root):
    root = Path(root)
    base = json.loads((root / "config.json").read_text())
    name = (root / "canary-name").read_text().strip()
    if not name.startswith("qdrant-sync-canary-"):
        raise RuntimeError("Refusing a non-canary source or target")
    mq = base["marqo_url"]

    def api(path, body=None, method="GET"):
        request = urllib.request.Request(mq + path, data=None if body is None else json.dumps(body).encode(),
                                         method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=240) as response:
            data = response.read()
            return json.loads(data) if data else {}

    try:
        api("/indexes/" + name + "/settings")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise RuntimeError("Canary source already exists; refusing reuse")
    client = QdrantClient(url=base["qdrant_url"], timeout=180, check_compatibility=False)
    assert name not in {c.name for c in client.get_collections().collections}
    settings = json.loads((root / "source-settings.json").read_text())
    settings["allFields"].append({"name": "query_enabled", "type": "bool", "features": ["filter"]})
    api("/indexes/" + name, settings, "POST")
    owner = root / "canary-owned.json"
    write_json(owner, {"collection": name, "created_at": time.time()})

    class Dimensions:
        dense_dim = 1024

    QdrantStore(client=client, embedder=Dimensions()).create_index(name, {})
    actual_settings = api("/indexes/" + name + "/settings")
    config = {**base, "source_index": name, "source_schema": "marqo__" + name.replace("-", "_01"),
              "target_collection": name, "ownership_marker": str(owner), "state_dir": str(root / "canary-state"),
              "source_settings_sha256": digest(actual_settings), "min_source_points": 1, "max_delete_fraction": 1}
    mirror = Mirror(config, client)
    workflow = "mirror-canary-workflow"
    records = [{"_id": uuid.uuid4().hex, "doc_id": "mirror-canary-document", "workflow_id": workflow,
                "instance": "amul", "chunk_num": n, "text": "Cow mastitis canary passage " + str(n),
                "text_for_embedding": "passage: Cow mastitis canary passage " + str(n),
                "is_reference": False, "query_enabled": True} for n in range(2)]
    anchor = {**records[0], "_id": uuid.uuid4().hex, "doc_id": "mirror-canary-anchor", "workflow_id": "other-workflow"}

    def add(rows):
        result = api("/indexes/" + name + "/documents", {"documents": rows}, "POST")
        assert not result.get("errors"), result

    add([*records, anchor])
    initial = mirror.cycle()
    assert initial["upserts"] == 3 and initial["payloads_verified"] == 3
    ids = [record_id_to_point_id(row["_id"]) for row in records]
    before = client.retrieve(name, ids, with_payload=True, with_vectors=True)
    original = {str(point.id): point.vector for point in before}
    for row in records:
        row["query_enabled"] = False
    add(records)
    hidden = mirror.cycle()
    assert hidden["metadata_updates"] == 2 and hidden["upserts"] == 0
    visibility = models.Filter(must=[models.FieldCondition(key="query_enabled", match=models.MatchValue(value=True)),
                                    models.FieldCondition(key="doc_id", match=models.MatchValue(value="mirror-canary-document"))])
    hits = client.query_points(name, query=[before[0].vector["dense"][0]], using="dense", query_filter=visibility).points
    assert not hits
    assert all(p.vector == original[str(p.id)] for p in client.retrieve(name, ids, with_vectors=True))
    for row in records:
        row["query_enabled"] = True
    add(records)
    shown = mirror.cycle()
    assert shown["metadata_updates"] == 2 and shown["upserts"] == 0
    hits = client.query_points(name, query=[before[0].vector["dense"][0]], using="dense", query_filter=visibility).points
    assert len(hits) == 2
    assert all(p.vector == original[str(p.id)] for p in client.retrieve(name, ids, with_vectors=True))
    records[0]["text"] = "Cow milk fever calcium canary edited passage"
    records[0]["text_for_embedding"] = "passage: " + records[0]["text"]
    add([records[0]])
    edited = mirror.cycle()
    assert edited["upserts"] == 1
    source = api("/indexes/" + name + "/documents?expose_facets=true", [records[0]["_id"]])["results"][0]
    dense = [f["_embedding"] for f in source["_tensor_facets"] if f.get("text_for_embedding")]
    actual = client.retrieve(name, [ids[0]], with_vectors=True)[0]
    assert len(actual.vector["dense"]) == len(dense)
    assert max(abs(float(a) - float(b)) for v, w in zip(actual.vector["dense"], dense)
               for a, b in zip(v, w)) < 1e-5
    api("/indexes/" + name + "/documents/delete-batch", [row["_id"] for row in records], "POST")
    waiting = mirror.cycle()
    assert waiting["deletes"] == 0 and waiting["pending_deletes"] == 2
    assert not client.query_points(name, query=[before[0].vector["dense"][0]], using="dense", query_filter=visibility).points
    deleted = mirror.cycle()
    assert deleted["deletes"] == 2 and deleted["target_points"] == 1
    assert client.retrieve(name, [record_id_to_point_id(anchor["_id"])])[0].payload["workflow_id"] == "other-workflow"
    client.delete_collection(name)
    api("/indexes/" + name, method="DELETE")
    report = {"passed": True, "source": name, "target": name, "new_chunks_copied": 3,
              "hide_show_preserved_vectors": True, "hidden_chunks_excluded_from_search": True,
              "edited_dense_vectors_equal_source": True, "chunk_numbers_preserved": True,
              "confirmed_workflow_deletion_kept_other_workflow": True, "temporary_indexes_removed": True}
    write_json(root / "canary-result.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    import sys
    run(sys.argv[1])
