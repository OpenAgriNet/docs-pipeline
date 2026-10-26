# Native Qdrant ingestion and DEV retrieval — 2026-10-26

This supersedes the temporary Marqo-to-Qdrant mirroring arrangement in
[qdrant-dev-cutover-20261026.md](qdrant-dev-cutover-20261026.md).

The user explicitly selected **direct Qdrant ingestion and DEV testing** and
excluded production changes. H100 pipeline API and worker now use
`VECTOR_STORE_BACKEND=qdrant`, targeting `amul-veterinary-final-20261023`.
DEV chat/voice retain their Qdrant BPP route. No production host, configuration,
index, or gateway route was modified during this native-writer deployment.
The existing Marqo index is retained, but this pipeline no longer updates it.

## Effective path

- Upload → existing MinIO/Temporal/OCR/translation/chunking/review → native Qdrant
  adapter → final collection.
- Dense sentence-window embeddings → existing authenticated H100 GPU E5 service
  at private port 8018, `/v1/embeddings`. The model, raw vector normalization,
  dimensions, prefixes and Dot/MaxSim schema match the final collection.
- Sparse BM25 is computed in the pipeline using the existing cached model.
- DEV public chat/voice → VM3 gateway → VM2 unified app → VM5 seeker/BPP → the
  H100 query service → the same final collection.

Both API and worker have identical backend/target/embedding settings. The stored
canonical index name remains `amul-veterinary-rebuild-20260821`, with an explicit
mapping to the final Qdrant collection; tenant ownership and registry rows are
preserved. Other logical tenant indexes keep their own identities and are not
silently merged into this collection.

The mirror and read bridge are stopped, have Docker restart policy `no`, and
are behind the `retired-mirror` Compose profile. **Do not restart the mirror:**
Marqo is no longer authoritative for new data and reconciliation could overwrite
or remove native Qdrant changes.

Search runs in explicit `QDRANT_INGESTION_MODE=native`; health/search validate an
owned native-writer configuration marker and the live collection, rather than
requiring a recently successful Marqo synchronization scan. Bearer authentication
and private binding remain. The new embedding endpoint uses the same bearer auth,
validates model/input bounds and serializes GPU access with retrieval. The HTTP
embedding client splits sentence windows into requests of at most 64, preserving
result order and rejecting missing/duplicate response indices.

## Narrow deployment and recovery

Private H100 directory: `/home/aicloud/qdrant-native-dev-20261026`.

The original image already contained an older optional Qdrant implementation.
A copied runtime source tree received only the reviewed backend modules and
specific DB mapping functions via `ops/qdrant_native_source.py`. Unrelated DB
fingerprint/schema changes were excluded. API/worker retain their exact original
image IDs, ports, networks, original mounts and non-backend environment values.
Authentication remains enabled. The embedding cache and read-only source overlay
are additional mounts.

- `documents.before.db`: quiescent SQLite backup, `quick_check=ok`.
- Native Qdrant snapshot plus `snapshot-backup.json`: 1.9 GB collection backup,
  size and SHA256 recorded before native mutations.
- `pipeline-before.json`, original search/store/mirror Compose files and source:
  private rollback evidence; these include sensitive settings and are not in Git.
- `pipeline.compose.json`, `apply-pipeline.sh`: exact preserved native API/worker
  deployment. The private, ignored `/home/aicloud/docs-pipeline/docker-compose.override.yml`
  also persists this configuration for normal Compose operations.
- `rollback-writer.sh` and `pipeline-rollback.compose.json`: restore the original
  pipeline writer only. This does **not** reverse native Qdrant data, redirect DEV
  retrieval, or authorize resuming mirroring. Remove/park the native managed
  override when intentionally returning to the old writer. A backend/data-authority
  rollback must account for any new Qdrant-only documents first.
- `native-writer.json`: explicit final-collection ownership/configuration marker.
- `runtime-preservation.json`, `native-tests-final.log`, `native-canary-result.json`:
  runtime audit and validation evidence.

The original GPU service Compose remains in
`/home/aicloud/qdrant-dev-deploy-20261024/compose.json`, now configured for native
mode. Qdrant's store Compose remains in
`/home/aicloud/qdrant-dev-cutover-20261026/store-compose.json`, with an additional
internal Docker network alias for API/worker writes. The host Qdrant port remains
loopback-only. No public database port was opened.

Use the native deployment files for maintenance. Historical mirror-based
readiness reports and scripts describe the previous phase and must not be used
as authority to restart source synchronization.

## Verification

- **160 tests passed** in the exact original pipeline image, covering adapter/SDK,
  tenant mapping, ingestion activities, native service mode, embedding request
  batching and preservation of unrelated runtime schema during backport.
- A disposable CSV went through the actual upload handler, MinIO and live Temporal
  worker, including parsing, translation/chunking/tagging and direct Qdrant ingest.
  Two new chunks appeared in the final collection with 1024-dimensional dense
  sentence vectors. The canary's test-process-only principal did not disable or
  modify authentication on the live API.
- Real handlers and worker reingestion verified text edits, chunk exclude/include,
  stable chunk numbering, document hide/show and scoped chunk/document deletion.
  Edits/exclusion follow the existing pipeline's explicit reingestion policy.
- The **registered DEV chat search tool** retrieved the newly uploaded Qdrant-only
  synthetic document through seeker → BPP. It was not copied into Marqo.
- Public HTTPS English/Gujarati chat returned HTTP 200 in about 8/12 seconds,
  substantive answers, and persisted `search_documents` tool calls with
  source-bearing results. These are smoke checks, not a relevance benchmark.
- A metadata-only validation-label prompt was declined by the chat agent. A normal
  veterinary question returned a sourced answer. Direct tool invocation, rather
  than the LLM's choice to use synthetic metadata, established new-document access.
- Automatic approval rejected copying existing veterinary records into the canary;
  that operation did not run. The canary remained synthetic.
- The test document was removed from search through the normal deletion handlers.
  Final collection count returned to **38,161**; its disabled SQLite audit record
  and test artifacts remain for traceability.

VM2 synthetic public request/history reports are under
`/home/azureuser/qdrant-native-dev-20261026`. The existing startup tenant-reconcile
warning is also present in the original image: `app.py` shadows the tenant service
with the tenant router. It is non-fatal and separate from this backend switch.

No production promotion, production verification, telephone/STT test, or claim
that Qdrant relevance exceeds Marqo is made by these checks.
