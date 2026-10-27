# H100 docs-pipeline record

Verified read-only on 2026-10-27. This record covers the docs-pipeline API and
worker. It contains no application routing, BPP changes or production rollout.

## Effective configuration

Both `docs-pipeline-api-1` and `docs-pipeline-worker-1` write directly to Qdrant
collection `amul-veterinary-final-20261023`. The registry keeps the canonical
name `amul-veterinary-rebuild-20260821`, resolved through `QDRANT_INDEX_MAP`.
Marqo remains the code fallback for deployments without an explicit backend;
it is not the selected backend for these H100 processes.

[The non-secret configuration example](../deploy/h100-qdrant.env.example)
records their backend and embedding settings. Dense vectors come from the
authenticated H100 E5 endpoint; sparse BM25 vectors are generated in the pipeline.
The installed versions are `qdrant-client==1.19.1` and `fastembed==0.8.0`, now
also pinned in this PR. Credentials remain private.

## Source correspondence

The running processes retain image
`sha256:96bc726654b4a737defa75eef651ad552f4e143122fe3e53a8cd7f18318a2d59`.
They mount `/home/aicloud/qdrant-native-dev-20261026/source/pipeline` read-only
at `/app/pipeline`; the host checkout is `7a7d64c`. This is a backport onto an
older runtime, not a deployment of the complete PR branch or current main.

API and worker Python sources match each other. Against the pre-switch archive,
normalizing line endings, exactly nine files differ. Eight match the PR code:
`embedding.py`, `vector_store.py`, `vector_store_qdrant.py`, `routers/search.py`,
`routers/tenants.py`, `services/access.py`, `services/indexes.py`, and
`temporal/document_tasks.py`.

The ninth, `db.py`, applies only the Qdrant mapping changes: tenant default/index
resolution, search settings mapping, reverse ownership lookup and canonical-name
persistence. Its older initialization, fingerprint/dedup and document lifecycle
code was preserved. Those baseline differences are not additional Qdrant changes;
copying the full PR `db.py` alone would mix schema versions.

## Deployment boundary

The existing API/worker were recreated using private `pipeline.compose.json` in
`/home/aicloud/qdrant-native-dev-20261026`. An automatic
`/home/aicloud/docs-pipeline/docker-compose.override.yml` also persists the
settings. These files include private configuration and are not copied into Git.
The original database/storage mounts and authentication were retained; a source
overlay and embedding cache mount were added.

The environment example is not a complete deployment or an apply script. A
reviewed deployment must explicitly carry these settings and required mounts
while preserving the destination's storage and authentication. This PR does not
replace the live containers. Other H100 services and retired migration tools are
outside this pipeline record.
