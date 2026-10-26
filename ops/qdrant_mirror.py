"""Mirror one explicitly owned Marqo source into a DEV Qdrant collection.

Marqo remains authoritative. Dense vectors come from its exported tensor facets,
never from a second embedding run. No import-time or implicit collection creation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline.vector_store_qdrant import _payload_from_record, record_id_to_point_id


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def projected_fields(settings):
    fields = {"marqo__id": ("_id", "text")}
    for field in settings["allFields"]:
        name, features = field["name"], field.get("features", [])
        key = ("marqo__filter_" + name if "filter" in features else
               "marqo__lexical_" + name if "lexical_search" in features else name)
        fields[key] = (name, field["type"])
    return fields


def decode_row(fields, mapping):
    return {mapping[key][0]: (bool(value) if mapping[key][1] == "bool" else value)
            for key, value in fields.items() if key in mapping}


def eligible_deletes(missing, previous, source_count, target_count, fraction):
    if target_count and len(missing) > max(1, int(target_count * fraction)):
        raise RuntimeError("Deletion safety limit exceeded; refusing automatic deletion")
    # A complete, stable second source scan is required in a subsequent cycle.
    return sorted(set(missing) & set(previous))


class Mirror:
    def __init__(self, config, client):
        self.config = config
        self.client = client
        self.root = Path(config["state_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.target = config["target_collection"]
        marker = json.loads(Path(config["ownership_marker"]).read_text())
        if marker.get("collection") != self.target:
            raise RuntimeError("Target ownership marker does not match collection")
        self.sparse = None

    def api(self, path, body=None):
        request = urllib.request.Request(
            self.config["marqo_url"] + "/indexes/" + self.config["source_index"] + path,
            data=None if body is None else json.dumps(body).encode(), method="GET",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)

    def scan(self, settings):
        mapping = projected_fields(settings)
        yql = ("select " + ", ".join(mapping) + " from " + self.config["source_schema"]
               + " where true order by marqo__id asc")
        records, expected = {}, None
        for offset in range(0, self.config.get("max_source_points", 200000), 500):
            query = urllib.parse.urlencode({"yql": yql, "hits": 500, "offset": offset})
            with urllib.request.urlopen(self.config["vespa_url"] + "/search/?" + query, timeout=120) as response:
                root = json.load(response)["root"]
            if root.get("errors"):
                raise RuntimeError("Source scan failed: " + str(root["errors"]))
            count = root["fields"]["totalCount"]
            if expected is None:
                expected = count
            if count != expected:
                raise RuntimeError("Source count changed during scan")
            children = root.get("children", [])
            for child in children:
                record = decode_row(child["fields"], mapping)
                rid = record["_id"]
                if rid in records:
                    raise RuntimeError("Duplicate source ID during scan")
                if record.get("instance") != self.config["tenant"]:
                    raise RuntimeError("Unexpected tenant in source collection")
                records[rid] = record
            if len(records) == expected:
                break
            if not children:
                raise RuntimeError("Incomplete source page")
        if len(records) != expected or expected < self.config["min_source_points"]:
            raise RuntimeError("Source scan incomplete or below minimum safe count")
        if self.api("/stats")["numberOfDocuments"] != expected:
            raise RuntimeError("Source count changed after scan")
        return records

    def target_payloads(self):
        records, offset = {}, None
        while True:
            points, offset = self.client.scroll(self.target, limit=500, offset=offset, with_payload=True, with_vectors=False)
            for point in points:
                payload = point.payload or {}
                rid = payload.get("record_id")
                if not rid or str(point.id) != record_id_to_point_id(rid) or payload.get("instance") != self.config["tenant"]:
                    raise RuntimeError("Target contains points outside the declared source/tenant namespace")
                records[rid] = payload
            if offset is None:
                return records

    def snapshot_before_mutation(self):
        path = self.root / "baseline-snapshot.json"
        if not path.exists():
            snapshot = self.client.create_snapshot(self.target, wait=True)
            write_json(path, {"collection": self.target, "snapshot": snapshot.name, "created_at": time.time()})

    def upsert(self, ids):
        from fastembed import SparseTextEmbedding
        from qdrant_client import models

        if self.sparse is None:
            self.sparse = SparseTextEmbedding(model_name="Qdrant/bm25", cache_dir=self.config["cache_dir"], local_files_only=True)
        for start in range(0, len(ids), 16):
            batch = ids[start:start + 16]
            rows = self.api("/documents?expose_facets=true", batch)["results"]
            if len(rows) != len(batch):
                raise RuntimeError("Incomplete tensor export")
            points = []
            for rid, row in zip(batch, rows):
                if not row.pop("_found", False) or row.get("_id") != rid:
                    raise RuntimeError("Source changed during tensor export")
                if row.get("instance") != self.config["tenant"]:
                    raise RuntimeError("Tensor export crossed tenant boundary")
                facets = row.pop("_tensor_facets", [])
                dense = [f["_embedding"] for f in facets if f.get("text_for_embedding") and len(f.get("_embedding", [])) == 1024]
                if not dense:
                    raise RuntimeError("Source tensor facets missing; refusing to synthesize replacement vectors")
                text = (str(row.get("text") or "") + " " + str(row.get("description") or "")).strip()
                sparse = next(iter(self.sparse.embed([text])))
                points.append(models.PointStruct(id=record_id_to_point_id(rid), payload=_payload_from_record(row),
                    vector={"dense": dense, "bm25": models.SparseVector(indices=sparse.indices.tolist(), values=sparse.values.tolist())}))
            self.client.upsert(self.target, points, wait=True)

    def cycle(self):
        started = time.time()
        settings = self.api("/settings")
        if digest(settings) != self.config["source_settings_sha256"]:
            raise RuntimeError("Source settings changed; schema/model must be reviewed before mirroring")
        source = self.scan(settings)
        target = self.target_payloads()
        expected = {rid: _payload_from_record(row) for rid, row in source.items()}
        missing = sorted(set(target) - set(source))
        state_path = self.root / "state.json"
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        deletes = eligible_deletes(missing, previous.get("pending_deletes", []), len(source), len(target), self.config.get("max_delete_fraction", .1))
        # Recheck authoritative IDs before any destructive operation. A count alone
        # cannot detect a simultaneous add/delete pair.
        if deletes and set(self.scan(settings)) != set(source):
            raise RuntimeError("Source IDs changed; refusing deletion")
        changed = sorted(rid for rid, payload in expected.items() if target.get(rid) != payload)
        vector_keys = ("text", "text_for_embedding", "description")
        reembed = [rid for rid in changed if rid not in target or any(target[rid].get(k) != expected[rid].get(k) for k in vector_keys)]
        metadata = sorted(set(changed) - set(reembed))
        staged = [rid for rid in missing if rid not in deletes and
                  (target[rid].get("query_enabled") is not False or not target[rid].get("_mirror_pending_delete"))]
        if changed or deletes or staged:
            self.snapshot_before_mutation()
        self.upsert(reembed) if reembed else None
        for rid in metadata:
            self.client.overwrite_payload(self.target, expected[rid], points=[record_id_to_point_id(rid)], wait=True)
        if staged:
            self.client.set_payload(self.target, {"query_enabled": False, "_mirror_pending_delete": True},
                                    points=[record_id_to_point_id(rid) for rid in staged], wait=True)
        if deletes:
            self.client.delete(self.target, points_selector=[record_id_to_point_id(rid) for rid in deletes], wait=True)
        # Verify every payload and ID after applying the cycle, allowing only the
        # first-cycle deletion candidates awaiting confirmation.
        actual = self.target_payloads()
        errors = [rid for rid, payload in expected.items() if actual.get(rid) != payload]
        extras = set(actual) - set(expected)
        if errors or not extras.issubset(set(missing) - set(deletes)):
            raise RuntimeError("Post-sync payload/ID verification failed")
        result = {"ok": True, "source": self.config["source_index"], "collection": self.target,
                  "source_points": len(source), "target_points": len(actual), "payloads_verified": len(expected),
                  "upserts": len(reembed), "metadata_updates": len(metadata), "deletes": len(deletes),
                  "staged_deletions": len(staged),
                  "pending_deletes": len(extras), "started_at": started, "last_success_at": time.time(),
                  "seconds": round(time.time() - started, 3)}
        write_json(state_path, {"pending_deletes": sorted(extras)})
        write_json(self.root / "status.json", result)
        print(json.dumps(result), flush=True)
        return result


def main():
    from qdrant_client import QdrantClient

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    root = Path(config["state_dir"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / "mirror.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mirror = Mirror(config, QdrantClient(url=config["qdrant_url"], timeout=180, check_compatibility=False))
        while True:
            try:
                mirror.cycle()
            except Exception as error:
                write_json(root / "status.json", {"ok": False, "error": str(error), "failed_at": time.time()})
                traceback.print_exc()
                if args.once:
                    raise
            if args.once:
                break
            time.sleep(config.get("interval_seconds", 30))


if __name__ == "__main__":
    main()
