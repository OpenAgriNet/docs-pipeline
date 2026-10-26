# Native DEV Qdrant ingestion

The current H100 API and worker write directly to the final Qdrant collection.
The mirror described below is retired and must remain stopped; running it over
native Qdrant data can overwrite or remove changes absent from Marqo.

`qdrant_dev_search.py` supports explicit `QDRANT_INGESTION_MODE=native` with an
owned `QDRANT_NATIVE_MARKER_PATH` matching the backend/collection and disabled
mirror. It checks the live collection without a Marqo freshness dependency.
The authenticated `/v1/embeddings` endpoint reuses its GPU model for pipeline
writes. Other deployments keep the existing mirror mode by default.

`qdrant_native_source.py` prepares a backend-only overlay from a copied runtime,
preserving unrelated DB schema/functions. It must run against staging copies.

Current deployment, tests and recovery:
[native DEV report](../docs/qdrant-native-dev-20261026.md).

# Historical DEV source synchronization

`qdrant_mirror.py` keeps one explicitly owned DEV collection aligned with an
authoritative Marqo index. It exports existing dense tensor facets; it does not
compute replacement dense embeddings. BM25 sparse vectors use text plus description.

The source settings hash, tenant, ownership marker, schema name, minimum count,
and deletion fraction are mandatory deployment decisions. The worker refuses
schema/model changes, mixed tenant/ID namespaces, incomplete scans, and excessive
deletion. It does not create or recreate the target collection. `--once` performs
a reconciliation and **can write**; it is not a dry run. A process lock prevents
overlapping workers for the same state directory.

Each cycle reads metadata directly through `vespa_read_proxy.py`, validates source
counts, compares every Qdrant payload, then copies tensors only for new/edited
chunks. Metadata updates preserve vectors. After a complete scan identifies a
missing record, it is hidden from search. Hard deletion requires its continued
absence in another cycle and a second stable source-ID scan. Reappearing records
are restored. A native snapshot is retained before the first mutation.

`qdrant_mirror_health.py` validates the atomic status report. The deployed
`qdrant_dev_search.py` refuses health/search with HTTP 503 if that report fails,
belongs to another collection, is incomplete, or is older than 120 seconds.
Bearer authentication remains mandatory. The read proxy publishes only on host
loopback and accepts read-only source-schema search requests.

The current H100 deployment polls 30 seconds after each completed scan. A full
38,161-record scan takes approximately 12 seconds, giving typical propagation
within about 45 seconds. Removed records are hidden after that first scan and
hard-deleted after confirmation, typically within about 90 seconds. This is
eventual synchronization, not an atomic dual write or native Qdrant ingestion.
Bulk deletions above the configured 10% guard or source counts below 30,000 stop
automatic reconciliation for review rather than deleting the target blindly.

`qdrant_mirror_canary.py` is an explicit live test. It refuses existing/non-canary
source names, creates isolated source and target indexes, and checks ingestion,
visibility, exact copied vectors, edits, chunk IDs, and scoped deletion. Successful
tests remove only their owned test indexes. Failed tests retain their artifacts
for diagnosis; do not broadly delete collections to retry them.

Deployment, artifacts and rollback:
[DEV cutover report](../docs/qdrant-dev-cutover-20261026.md).
