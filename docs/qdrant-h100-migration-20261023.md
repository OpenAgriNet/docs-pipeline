# H100 Qdrant migration preparation — 2026-10-23

The H100 operation directory is `/home/aicloud/qdrant-migration-final-20261023`.
Long jobs run in detached Docker containers and continue after the laptop or SSH
connection closes. Both collection builds and the queued integration/restore
checks completed successfully; the server artifacts retain the evidence.

## Candidate collections

- `amul-veterinary-final-20261023`: complete 38,161-record rebuild corpus from
  Marqo `amul-veterinary-rebuild-20260821`.
- `amul-veterinary-live-compat-20261023`: separate 14,993-record copy of the
  currently served DEV `amul-veterinary-index-restored`. This preserves the option to change backend without changing corpus.

Both use an isolated Qdrant 1.13.2 instance on H100 loopback port 6336 with its
own persistent Docker volume. Existing Marqo and Qdrant services remain in place.
The copy exports Marqo's actual sentence vectors rather than regenerating them;
chunk IDs and payloads are preserved. BM25 covers text and description. Compressed
source batches, manifests, checkpoints, logs, and per-point validation stay on H100.

## Verification

- Targeted backend, tenant, ingestion, and lifecycle suite: 148 passed.
- Full suite: 1,016 passed, one existing UI-source assertion failed. The pre-change
  baseline has the same failure (`test_console_offers_tenants_to_a_pure_platform_admin`).
- Actual Qdrant server canary: semantic/lexical/hybrid search, tenant filter,
  hide/show with unchanged vectors, and workflow-scoped removal of 1,105 records
  passed. Its disposable collection was removed.
- The first real tests caught a multivector query-shape bug. Dense queries now use
  a one-row matrix, as required by MaxSim.
- The deployed BPP source was recovered from DEV at `5b8a7cf`. Its isolated
  Qdrant candidate and tests live under `bpp-source/` in the H100 operation
  directory. The candidate compiles, its 24 unit tests pass, and its two real
  integration tests pass: English/Gujarati retrieval through an isolated BPP
  with paired Beckn callbacks, and Marqo → Qdrant → Marqo retrieval rollback.
  This candidate is not an upstream BPP PR: current GitHub credentials cannot
  access `OpenAgriNet/amul-bpp`.

## Completed collection checks

- Main collection: 38,161 points and 190,341 sentence vectors; every copied ID,
  payload, and dense vector was verified against the frozen source snapshot.
- Live-compat collection: 14,993 points and 81,475 sentence vectors, likewise
  fully verified. The larger candidate remains the main migration collection.
- A full 38,161-record source recheck found no payload drift during the copy.
- Native snapshot: `amul-veterinary-final-20261023.snapshot`, 964,044,288 bytes,
  SHA-256 `07f6012223d319af60bc5c1e9bec1618cfb01eb831370b815c63f2bf7d0e919b`.
  Restoring it into a disposable collection recovered all 38,161 points; 20
  sampled IDs/payloads/vectors matched. The disposable restore was removed.
- GPU query probe: E5 produced 1,024-dimensional raw vectors with CUDA enabled.

The bilingual tests used the actual candidate collection and recovered BPP
source, with an isolated in-memory callback queue and captured callbacks. They
do not establish full live chat/voice → seeker → ONIX → deployed BPP behavior,
Redis durability, signing, or production readiness. Those remain rollout checks.

## Resume and completion evidence

Inspect `status.json`, `build.log`, `tests-v2.log`, `full-tests-v2.log`,
`baseline-tests-v2.log`, `server-canary.json`, `collection-complete.json`,
`bpp-unit.log`, `bpp-integration.log`, `source-drift-check.json`,
`snapshot-restore.json`, and `search-service.log` in that directory.
The live-compat collection keeps separate artifacts under `live-compat/`.
A missing completion file means the corresponding step is not confirmed complete.

Containers have names beginning `docs-qdrant-final-`, plus
`docs-qdrant-migration-final-20261023` and
`docs-qdrant-live-compat-build-20261023`. Do not remove them or their volumes while
work is running. The GPU search service uses an otherwise idle H100 GPU, starts
only after collection validation, and binds loopback port 8016. It is an isolated
raw retrieval endpoint; BPP retains its own reranker, diversity/backfill, and
Beckn catalog/callback handling.

## DEV switch and rollback boundary

No live chat, voice, BPP, or production route has been switched. Before DEV
cutover, resolve BPP repository access, put its candidate and deployment changes
through review, verify dataset selection and source freshness, retain the completed
integration evidence, and configure private authenticated connectivity to the query
service. A loopback-only test service is not a production deployment.

The DEV BPP defaults to Marqo. Preserve its exact original image, environment,
Compose overlays, signing route, and Redis settings; do not recreate it from a
base Compose file alone. A private pre-change inspect snapshot is kept on DEV
under `/home/azureuser/qdrant-cutover-prep-20261023/`. The candidate's opt-in
`SEARCH_BACKEND=qdrant` uses `QDRANT_SEARCH_URL`; returning to `SEARCH_BACKEND=marqo`
(or its default) restores the original Marqo path and index settings. The queued
integration test rehearses Marqo → Qdrant → Marqo retrieval on isolated clients.
Actual chat/voice end-to-end DEV testing follows a separately authorized managed
DEV switch. Production requires its own verified route/configuration and rollout.
