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
| `EMBEDDING_NORMALIZE` | L2 normalization, disabled by default to match the current Marqo index. |
| `SPARSE_EMBEDDING_MODEL` | `Qdrant/bm25` by default. |

Use an explicit map for collection names that do not share the inferred suffix.
Mappings are applied to search, ingestion, access checks, provisioning, stats,
and deletion. Tenant registry rows and saved search settings keep canonical
names so selecting Marqo again does not require rewriting them.

## Retrieval and safety

Each original pipeline chunk remains one Qdrant point with the same ID and
payload. The `dense` vector holds overlapping three-sentence windows (window
size 3, overlap 1), scored with Qdrant MaxSim so the best-matching window ranks
the original chunk. Dense embeddings use the E5 `passage:` / `query:` prefixes,
raw vectors, and Dot distance to match Marqo's `normalizeEmbeddings=false`
setting. The `bm25` vector indexes the chunk text and description, with Qdrant's
BM25 IDF modifier enabled. Replacement ingest checks the live vector names,
dimension, MaxSim/Dot configuration, and sparse modifier before purge.
The Qdrant server must support multivectors (v1.10 or newer).

The pipeline's shared search settings select the same three modes for either
store: `TENSOR`, `LEXICAL`, and `HYBRID`. Qdrant hybrid search retrieves dense
and BM25 candidate lists separately, then applies the configured semantic
`alpha` and `rankingMethod` (`rrf` or `normalize_linear`) before the shared
application reranker and per-document chunk limit. Qdrant's `rrfK` is used as
the reciprocal-rank constant and candidate-pool floor. The fusion is an adapter
implementation over Qdrant's returned scores/ranks, so it is behaviorally
aligned with the pipeline settings but not guaranteed bit-for-bit identical to
Marqo's internal scoring.

The shared reranking, query expansion, reference/tag filters, result limit, and
per-document diversity logic run in the pipeline API. Chat and voice backends
have their own search integrations; selecting Qdrant here does not redirect
those consumers. The sentence splitter uses punctuation and line breaks; it
matches Marqo's configured window size and overlap but may differ on edge cases
such as abbreviations. Existing single-vector Qdrant collections are not
compatible with this schema and need a fresh shadow collection plus reingest.

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
