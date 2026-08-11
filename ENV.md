# Environment variables — Frontend & Backend

Single reference for every environment variable used by the docs-pipeline **UI (FE)** and **API / worker (BE)**.  
Sources: `ui/.env.example`, root `.env.example`, and runtime `os.environ` / `import.meta.env` reads in code.

---

## Frontend (Vite / `ui/`)

Anything the browser needs **must** be prefixed with `VITE_`.  
Load from `ui/.env` (local) or build-time env. See also `ui/.env.example`.

| Variable | Default | Required when | Purpose |
|----------|---------|---------------|---------|
| `VITE_API_PROXY_TARGET` | `http://api:8001` (compose) / use `http://localhost:8001` locally | Dev server | Vite proxy target for `/api` → FastAPI |
| `VITE_AUTH_ENABLED` | `false` | — | `true` shows the login page and requires SSO; `false` opens the app without login |
| `VITE_KEYCLOAK_URL` | `''` | Auth on | Keycloak base URL (include `/auth` if used). Production: `https://auth-vistaar.da.gov.in/auth` |
| `VITE_KEYCLOAK_REALM` | `''` | Auth on | Realm name. Production: `bharat-vistaar` |
| `VITE_KEYCLOAK_CLIENT_ID` | `docs-pipeline-ui` | Auth on | Public OIDC client. Production shared client: `bharat-vistaar` |
| `VITE_KEYCLOAK_IDP_HINT` | `google` | Optional | Identity-provider hint for SSO pop-up (`google`, etc.) |

### Frontend auth notes

- Login UI is ported from **vistaar-platform** (`/login`, hero panel, “Continue with SSO” pop-up).
- SSO callback route: `/auth/sso-callback`.
- Register these **Valid Redirect URIs** on the Keycloak client:
  - `http://localhost:<port>/login`
  - `http://localhost:<port>/auth/sso-callback`
  - `https://<your-app-origin>/login`
  - `https://<your-app-origin>/auth/sso-callback`
- When auth is enabled, the UI calls `GET /api/auth/me` with `Authorization: Bearer <token>`.  
  Backend must have `AUTH_DISABLED=false` and matching `KEYCLOAK_*` or the session will fail after SSO.

### Recommended production FE values

```bash
VITE_AUTH_ENABLED=true
VITE_KEYCLOAK_URL=https://auth-vistaar.da.gov.in/auth
VITE_KEYCLOAK_REALM=bharat-vistaar
VITE_KEYCLOAK_CLIENT_ID=bharat-vistaar
VITE_KEYCLOAK_IDP_HINT=google
VITE_API_PROXY_TARGET=http://localhost:8001   # or in-cluster API URL in compose
```

---

## Backend (API + worker)

Root `.env` (see `.env.example`). Required by FastAPI (`pipeline/api.py`), Temporal worker (`pipeline/worker.py`), and activities.

### Required

| Variable | Default | Purpose |
|----------|---------|---------|
| `MINIO_ACCESS_KEY` | *(required)* | MinIO access key |
| `MINIO_SECRET_KEY` | *(required)* | MinIO secret key |

### Core infrastructure

| Variable | Default | Purpose |
|----------|---------|---------|
| `TEMPORAL_HOST` | `localhost:7233` | Temporal gRPC address |
| `TEMPORAL_MAX_CONCURRENT_ACTIVITIES` | `4` | Worker activity concurrency |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO API host:port |
| `MINIO_BUCKET` | `documents` | Object storage bucket |
| `DOCUMENT_DB_PATH` | `/data/documents.db` | SQLite path (use `./data/documents.db` for local non-Docker) |
| `MARQO_URL` | `http://localhost:8882` | Marqo base URL |
| `MARQO_INDEX_NAME` | `documents-index` | Index name (compose/scripts; workflows often default to `documents-index`) |
| `LANG_DETECT_URL` | `http://lang-detect:3000` (compose) | Language detection service |

### API HTTP surface

| Variable | Default | Purpose |
|----------|---------|---------|
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated browser origins (add `http://localhost:3001` if UI uses that port) |
| `RATE_LIMIT_DEFAULT` | `100/minute` | Default rate limit |
| `RATE_LIMIT_UPLOAD` | `10/minute` | Upload rate limit |
| `ALLOWED_FILE_PATHS` | `/app/books,/data/documents` | Allowed local paths for path-based ingest |
| `DOCS_PIPELINE_API_URL` | request base URL | Public API base for provenance/links |
| `DOCS_PIPELINE_UI_URL` | `http://localhost:3000` | Public UI base for links |
| `DEFAULT_INSTANCE` | `default` | Default tenant / instance id |

### Auth / Keycloak (backend JWT validation)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTH_DISABLED` | `true` | `true` = bypass permissions (still reads name/email/roles from JWT when sent). `false` = enforce JWT validation |
| `KEYCLOAK_ISSUER` | `''` | **Must match FE realm** — JWT `iss` e.g. `https://auth-vistaar.da.gov.in/auth/realms/bharat-vistaar` |
| `KEYCLOAK_JWKS_URL` | derived from issuer | JWKS URL for the same realm |
| `KEYCLOAK_AUDIENCE` | `''` (skip aud) | Leave empty for SPA tokens; set only if you enforce a fixed `aud` claim |
| `KEYCLOAK_JWT_LEEWAY_SECONDS` | `30` | Clock-skew leeway for `exp`/`nbf` |
| `KEYCLOAK_ADMIN_USERNAME` | `''` | Super-admin **Users** UI: Keycloak admin username (often master `admin`) |
| `KEYCLOAK_ADMIN_PASSWORD` | `''` | Keycloak admin password for provisioning users |
| `KEYCLOAK_ADMIN_BASE_URL` | from `KEYCLOAK_ISSUER` | e.g. `https://dev-auth-vistaar.da.gov.in/auth` |
| `KEYCLOAK_ADMIN_REALM` | from issuer | Realm to manage (e.g. `bharat-vistaar`) |
| `KEYCLOAK_ADMIN_TOKEN_REALM` | `master` | Realm used to obtain admin token via `admin-cli` |

**FE ↔ BE pairing (required for SSO):**

| Frontend (`ui/.env`) | Backend (root `.env`) |
|----------------------|------------------------|
| `VITE_KEYCLOAK_URL` + `VITE_KEYCLOAK_REALM` | `KEYCLOAK_ISSUER` = `{URL}/realms/{REALM}` |
| same realm | `KEYCLOAK_JWKS_URL` = `{ISSUER}/protocol/openid-connect/certs` |
| `VITE_KEYCLOAK_CLIENT_ID=bharat-vistaar` | public client used by browser only |
| `VITE_AUTH_ENABLED=true` | send Bearer tokens; optional `AUTH_DISABLED=false` to enforce them |

Keycloak is an external instance (not provisioned by this repo's compose files) — see `KEYCLOAK_*` above for connecting to it.

### OCR (Chandra)

OCR is a **separate process** (not in `docker-compose`). The worker calls
`CHANDRA_VLLM_BASE_URL` (HF mode → `POST {base}/ocr/pages`).

| Variable | Default | Purpose |
|----------|---------|---------|
| `OCR_PROVIDER` | `chandra` | OCR provider (`chandra`, `mock`, `pypdf`) |
| `OCR_MODEL` | `chandra` | Model name |
| `CHANDRA_VLLM_BASE_URL` | `''` | Base URL; local HF default `http://localhost:8010/v1` |
| `CHANDRA_OCR_API_URL` | `''` | Alternate full OCR API URL (overrides base path build) |
| `CHANDRA_INFERENCE_MODE` | `hf` | `hf` = HTTP page API; `vllm` = chandra-ocr client + OpenAI base |
| `CHANDRA_MAX_OUTPUT_TOKENS` | `12288` | Max generation tokens |
| `CHANDRA_OCR_MAX_WORKERS` | `4` | Parallel OCR workers |
| `CHANDRA_IMAGE_DPI` | `192` | Page render DPI |
| `CHANDRA_REQUEST_TIMEOUT_SECONDS` | `300` | Request timeout |
| `OCR_MAX_SPLIT_PAGES` | `40` | Max split pages |
| `OCR_SEGMENT_PAGES` | `20` | Segment size for long docs |
| `CHANDRA_HF_HOME` / `HF_HOME` | — | Hugging Face cache (HF server / scripts) |

**Local start (real model, GPU + torch + model download):**

```bash
# requires: pip install chandra-ocr torch  (and enough VRAM/disk for Chandra weights)
python scripts/chandra_hf_server.py   # listens on :8010
```

**Local unblock without GPU (placeholder OCR):**

```bash
python scripts/mock_chandra_ocr_server.py   # same :8010 API surface as HF server
# or in-process (no HTTP server): OCR_PROVIDER=mock  # restart worker
```

### Translation (Gemma)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TRANSLATION_PROVIDER` | `gemma_vllm` | Provider |
| `TRANSLATION_MODEL` | `gemma-4-31b-it` | Model id |
| `TRANSLATION_VLLM_BASE_URL` | `http://localhost:8020/v1` | OpenAI-compatible endpoint |
| `TRANSLATION_API_KEY` | `''` | Optional API key |
| `TRANSLATION_PAGE_CONCURRENCY` | `1` | Parallel pages |
| `TRANSLATION_MAX_RETRIES` | `6` | Retry count |
| `TRANSLATION_RETRY_BASE_SECONDS` | `2.0` | Backoff base |
| `TRANSLATION_MAX_OUTPUT_TOKENS` | `8000` | Max tokens |
| `TRANSLATION_REQUEST_TIMEOUT_SECONDS` | `300` | Timeout |
| `TRANSLATION_SCRIPT_GATE_ENABLED` | `true` | Regex script gate: only pages with non-Latin (Indic) script are translated. `false` restores per-line lang-detect, which misreads OCR noise as European languages |
| `TRANSLATION_SCRIPT_MIN_CHARS` | `15` | Minimum non-Latin characters on a page before it counts as non-English |
| `TRANSLATION_SCRIPT_MIN_RATIO` | `0.05` | Minimum share of all letters that must be non-Latin |

### Domain tagging

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOMAIN_TAGGING_ENABLED` | `true` | Enable/disable |
| `DOMAIN_TAGGING_PROVIDER` | `gemma_vllm` | Provider |
| `DOMAIN_TAGGING_MODEL` | falls back to `TRANSLATION_MODEL` | Model |
| `DOMAIN_TAGGING_VLLM_BASE_URL` | falls back to `TRANSLATION_VLLM_BASE_URL` | Endpoint |
| `DOMAIN_TAGGING_API_KEY` | falls back to `TRANSLATION_API_KEY` | Key |
| `DOMAIN_TAGGING_STRICT_TAXONOMY` | `true` | Restrict tags to taxonomy |
| `DOMAIN_TAXONOMY_PATH` | package default | Path to taxonomy JSON |
| `DOMAIN_TAGGING_CONCURRENCY` | `4` | Parallelism |
| `DOMAIN_TAGGING_REQUEST_TIMEOUT_SECONDS` | `120` | Timeout |
| `DOMAIN_TAGGING_MAX_OUTPUT_TOKENS` | `1024` | Max tokens |

### Chunking

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHUNKING_PROVIDER` | `deterministic` (code) / often `qwen_vllm` in `.env` | Provider |
| `CHUNKING_MODEL` | provider name | Model id |
| `CHUNKING_VLLM_BASE_URL` | `''` | LLM endpoint |
| `CHUNKING_API_KEY` | `''` | Optional key |
| `CHUNKING_TARGET_CHUNK_TOKENS` | `450` | Target chunk size |
| `CHUNKING_MAX_CHUNK_TOKENS` | `450` | Max chunk size |
| `CHUNKING_MIN_CHUNK_TOKENS` | `100` | Min chunk size |
| `CHUNKING_OVERLAP_TOKENS` | `128` | Overlap |
| `CHUNKING_MAX_PAGES_PER_CHUNK` | `8` | Max page span |
| `CHUNKING_PAGE_WINDOW_SIZE` | `8` | Window size |
| `CHUNKING_QWEN_ENABLE_THINKING` | `false` | Qwen thinking mode |
| `CHUNKING_TEMPERATURE` | `0.0` | Sampling temperature |
| `CHUNKING_SEED` | `0` | Seed |
| `CHUNKING_FALLBACK_PROVIDER` | `deterministic` | Fallback provider |
| `CHUNKING_REQUEST_TIMEOUT_SECONDS` | `120` | Timeout |

### Metadata enrichment (worker)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOCUMENT_METADATA_CSV_PATH` | `/app/workspace/document_manifest.csv` | Optional manifest CSV |
| `DOCUMENT_DESCRIPTIONS_JSONL_PATH` | `/app/workspace/document_descriptions.jsonl` | Optional descriptions JSONL |

### Vector backend (Qdrant only)

`VECTOR_BACKEND` defaults to **`qdrant`**. Docker Compose no longer ships Marqo. Search, index status, deletes, and ingestion go through `pipeline/vector_store` → Qdrant. Legacy `VECTOR_BACKEND=marqo` still selects the Marqo store module for emergency rollback only (not in compose).

| Variable | Purpose |
|----------|---------|
| `VECTOR_BACKEND` | `qdrant` (default) or `marqo` (legacy emergency only) |
| `QDRANT_URL` | Qdrant base URL (e.g. `http://localhost:6333` or reverse-proxy HTTPS) |
| `QDRANT_API_KEY` | Qdrant API key (required for non-local hosts) |
| `QDRANT_COLLECTION_NAME` | Collection name (default `documents-index`) |
| `QDRANT_TIMEOUT_SECONDS` | Client timeout |
| `EMBEDDING_PROVIDER` | `sentence_transformers` (local) or `openai_compatible` |
| `EMBEDDING_MODEL` | Embedding model id (default `intfloat/multilingual-e5-large`) |
| `EMBEDDING_VECTOR_SIZE` | Vector dimensions (default `1024`) |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | Remote embeddings API when `openai_compatible` |

### Master Scheme Catalog (AI tool / prompt sync)

Exposes `/catalog/v1/*` so **bharat-oan-api** and **bharat-provider-backend** can refresh scheme lists and tool prompts without redeploying hard-coded registries.

| Variable | Default | Purpose |
|----------|---------|---------|
| `CATALOG_SERVICE_API_KEYS` | *(empty)* | Comma-separated service keys for `X-Catalog-Service-Key` (OAN/provider warmers). When empty and `AUTH_DISABLED=true`, catalog is open to admin/search JWT bypass. |
| `CENTRAL_INSTANCES` | `default` | Instances that default `network_visible=true` for new scheme docs |
| `PROD_QDRANT_URL` | — | PROD Qdrant for document promote |
| `PROD_QDRANT_API_KEY` | — | PROD Qdrant key |
| `PROD_QDRANT_COLLECTION_NAME` | `documents-index` | PROD collection for normal documents |
| `PROD_SCHEME_QDRANT_URL` | `PROD_QDRANT_URL` | PROD Qdrant for scheme promotes |
| `PROD_SCHEME_QDRANT_API_KEY` | `PROD_QDRANT_API_KEY` | Scheme collection API key |
| `PROD_SCHEME_QDRANT_COLLECTION_NAME` | `schemes-index` | Must **not** equal documents collection |

**Endpoints:** `GET /catalog/v1/snapshot`, `/version`, `/schemes`, `/tool-prompt`; `POST /catalog/v1/rebuild`, `/bootstrap`; `PATCH /documents/{id}/scheme-metadata`.

### Master Catalog — Postgres + Redis push (dev preview channel)

Separate from the SQLite/Qdrant catalog above (which tracks PROD vector publication and is scheme-specific). This one is a generic Postgres table (`master_catalog`: code, content_type, name, tool_name, doc_id, prompt_snippet, status) — not scheme-only, since ingested documents can be schemes, advisories, or other kinds that later grow their own code/name/tool metadata. docs-pipeline writes to it on **DEV ingest complete** (`status=dev`) and **PROD promote complete** (`status=live`), then pushes directly into the AI layer's Redis so bharat-oan-api can test an entry's prompt/tool routing in the dev chatbot without an AI-layer redeploy.

| Variable | Default | Purpose |
|----------|---------|---------|
| `MASTER_CATALOG_PG_HOST` | *(empty — required)* | Host of your existing Postgres instance. No DB container is provisioned by docker-compose for this — bring your own server and create the database on it (e.g. `CREATE DATABASE master_catalog`). Tables are created automatically on first sync. |
| `MASTER_CATALOG_PG_PORT` | `5432` | Postgres port |
| `MASTER_CATALOG_PG_DB` | `master_catalog` | Database name |
| `MASTER_CATALOG_PG_USER` / `MASTER_CATALOG_PG_PASSWORD` | `master_catalog` / *(empty)* | Credentials |
| `MASTER_CATALOG_PG_SSLMODE` | `disable` | Use `require` for managed prod Postgres |
| `AI_LAYER_REDIS_HOST` | *(empty)* | bharat-oan-api's Redis host. Empty = Postgres sync still happens, Redis push is skipped |
| `AI_LAYER_REDIS_PORT` / `AI_LAYER_REDIS_DB` / `AI_LAYER_REDIS_PASSWORD` | `6379` / `0` / *(empty)* | AI layer Redis connection |
| `MASTER_CATALOG_REDIS_TTL_SECONDS` | `172800` (48h) | Snapshot key TTL — a dead-man's switch, not a freshness mechanism (writes are push-driven) |

**Redis keys:** `master-catalog:dev:snapshot` (dev + live entries — what the dev chatbot reads), `master-catalog:live:snapshot` (live only — what prod reads). Plain JSON via raw `redis-py`, not routed through bharat-oan-api's `aiocache` layer, since it's an external write contract rather than an internal cache value.

---

## Who consumes what

| Concern | FE | API | Worker |
|---------|----|-----|--------|
| `VITE_*` proxies & Keycloak browser login | ✅ | — | — |
| JWT validation (`AUTH_*`, `KEYCLOAK_*`) | — | ✅ | — |
| Temporal / MinIO / SQLite | — | ✅ | ✅ |
| OCR / translation / chunking / domain tags | — | config / status | ✅ runs jobs |
| Qdrant (`VECTOR_BACKEND`, `QDRANT_*`) | — | ✅ | ✅ |

---

## Local non-Docker quick start

**Backend** (repo root):

```bash
set -a && source .env && set +a
export DOCUMENT_DB_PATH="$(pwd)/data/documents.db"
export ALLOWED_FILE_PATHS="$(pwd)/books,$(pwd)/data/documents"
export CORS_ORIGINS="http://localhost:3000,http://localhost:3001"
export PYTHONPATH="$(pwd)"
.venv/bin/uvicorn pipeline.api:app --host 0.0.0.0 --port 8001 --reload
```

**Frontend** (`ui/`):

```bash
# ui/.env should set VITE_API_PROXY_TARGET=http://localhost:8001
npm run dev -- --port 3001
```

With auth:

```bash
# ui/.env
VITE_AUTH_ENABLED=true
VITE_KEYCLOAK_URL=https://auth-vistaar.da.gov.in/auth
VITE_KEYCLOAK_REALM=bharat-vistaar
VITE_KEYCLOAK_CLIENT_ID=bharat-vistaar
VITE_KEYCLOAK_IDP_HINT=google

# root .env (API) — same realm as FE
AUTH_DISABLED=true
KEYCLOAK_ISSUER=https://auth-vistaar.da.gov.in/auth/realms/bharat-vistaar
KEYCLOAK_JWKS_URL=https://auth-vistaar.da.gov.in/auth/realms/bharat-vistaar/protocol/openid-connect/certs
KEYCLOAK_AUDIENCE=
```


---

## Files to keep in sync

| File | Role |
|------|------|
| `ENV.md` | This document (human-readable contract) |
| `.env.example` | Backend annotated template |
| `.env` | Local backend secrets (gitignored) |
| `ui/.env.example` | Frontend annotated template |
| `ui/.env` | Local frontend overrides (gitignored if configured) |
| `docker-compose.yml` | Compose-injected env for API, worker, UI |
