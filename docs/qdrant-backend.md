# Optional Qdrant backend (#151)

The default backend remains Marqo. This change supplies an opt-in adapter; it
does not populate a collection, change deployments, or move chat retrieval.
The veterinary Beckn Provider Platform (BPP) is a separate consumer and needs
its own backend integration and evaluation before chat can use Qdrant.

## Configuration contract

API and worker processes must receive the same backend and embedding settings
through their deployment configuration. Setting a host `.env` variable alone
does not inject it into an existing container. No Qdrant service or environment
changes are made to Compose in this first migration slice.

| Variable | Meaning |
| --- | --- |
| `VECTOR_STORE_BACKEND` | `marqo` (default) or `qdrant`; unknown values fail loudly. |
| `QDRANT_URL` | Endpoint reachable from the process/container. The code fallback is host-local `http://localhost:6333`; explicitly configure this inside containers. |
| `QDRANT_API_KEY` | Optional credential for an authenticated endpoint. |
| `QDRANT_INDEX_NAME` | Default Qdrant collection; otherwise the canonical Marqo default is used. |
| `QDRANT_INDEX_MAP` | Comma-separated canonical-name=collection-name mappings. |
| `QDRANT_INDEX_SUFFIX` | Optional suffix for tenant collections; otherwise inferred when the Qdrant default extends the Marqo default. |
| `EMBEDDING_BACKEND` | `fastembed` (default), or `http` for remote dense embeddings with local sparse embeddings. |
| `EMBEDDING_MODEL` | `intfloat/multilingual-e5-large` by default. |
| `EMBEDDING_URL` | OpenAI-compatible embeddings endpoint when using `http`. |
| `EMBEDDING_API_KEY` | Credential for the remote embeddings endpoint, when required. |
| `EMBEDDING_DIM` | Remote dense dimension, default 1024. Must match the actual model and collection. |
| `EMBEDDING_NORMALIZE` | L2 normalization, enabled by default. |
| `SPARSE_EMBEDDING_MODEL` | `Qdrant/bm25` by default. |

Use an explicit map for collection names that do not share the inferred suffix.
Mappings are applied to search, ingestion, access checks, provisioning, stats,
and deletion. Tenant registry rows and saved search settings keep canonical
names so selecting Marqo again does not require rewriting them.

## Retrieval and safety

Records use named `dense` and `bm25` vectors. Dense embeddings use the E5
`passage:` / `query:` prefixes and Cosine distance. Sparse vectors use raw text;
the collection must enable Qdrant's BM25 IDF modifier. Replacement ingest checks
the live vector names, dimension, distance, and sparse modifier before purge.

Hybrid retrieval uses Qdrant RRF fusion, followed by the existing application
reranking. It does not reproduce Marqo's alpha weighting; the existing `rrfK`
setting controls the candidate pool here, not Qdrant's fusion constant. Ranking
quality and latency must be measured separately before any cutover.

Domain tags use an exact keyword-array payload alongside the existing display
string. Metadata-only updates preserve vectors and unrelated payload. Visibility
patches return confirmed successes and individual failures, including missing
records. Workflow-scoped purge retains co-resident documents and pages until
all matching records are removed.

Local FastEmbed loads/downloads model weights on first use. The in-process
contract tests use a deterministic fake embedder and never download models or
contact inference services. They verify SDK filtering, updates, all three search
modes, scoped deletion of more than 1,000 records, and partial visibility failure.
They do not replace integration tests against the deployed Qdrant server.

## Validation and remaining migration work

The backend SDK pair is pinned to `qdrant-client==1.13.3` and `fastembed==0.6.1`.
Run the backend contracts and existing application regressions after installing
requirements:

```sh
python -m pytest tests/test_vector_store_qdrant.py tests/test_qdrant_local_contract.py \
  tests/test_tenant_indexes.py tests/test_vector_store.py tests/test_activities.py
```

Issue #151 still owns the snapshot-based, per-tenant reindex plan, genuinely
read-only inventory, fresh shadow collection, labelled real-chat-path retrieval
evaluation, and reviewed deployment/rollback. PR #145's parent/chunk visibility
semantics must be integrated and tested before relying on hide/show behavior.
Existing shadow data or old benchmark results are not cutover approval.
