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
| `VITE_MARQO_PROXY_TARGET` | `http://marqo:8882` (compose) / use `http://localhost:8882` locally | Dev server | Vite proxy target for `/marqo` |
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
VITE_MARQO_PROXY_TARGET=http://localhost:8882
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

**FE ↔ BE pairing (required for SSO):**

| Frontend (`ui/.env`) | Backend (root `.env`) |
|----------------------|------------------------|
| `VITE_KEYCLOAK_URL` + `VITE_KEYCLOAK_REALM` | `KEYCLOAK_ISSUER` = `{URL}/realms/{REALM}` |
| same realm | `KEYCLOAK_JWKS_URL` = `{ISSUER}/protocol/openid-connect/certs` |
| `VITE_KEYCLOAK_CLIENT_ID=bharat-vistaar` | public client used by browser only |
| `VITE_AUTH_ENABLED=true` | send Bearer tokens; optional `AUTH_DISABLED=false` to enforce them |

Compose-only Keycloak deploy vars (not read by FastAPI app code):  
`KEYCLOAK_PORT`, `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_DB_PASSWORD`, `KEYCLOAK_DB_DATA_PATH`.

### OCR (Chandra)

| Variable | Default | Purpose |
|----------|---------|---------|
| `OCR_PROVIDER` | `chandra` | OCR provider |
| `OCR_MODEL` | `chandra` | Model name |
| `CHANDRA_VLLM_BASE_URL` | `''` | OpenAI-compatible OCR endpoint |
| `CHANDRA_OCR_API_URL` | `''` | Alternate OCR API URL |
| `CHANDRA_INFERENCE_MODE` | `hf` | Inference mode |
| `CHANDRA_MAX_OUTPUT_TOKENS` | `12288` | Max generation tokens |
| `CHANDRA_OCR_MAX_WORKERS` | `4` | Parallel OCR workers |
| `CHANDRA_IMAGE_DPI` | `192` | Page render DPI |
| `CHANDRA_REQUEST_TIMEOUT_SECONDS` | `300` | Request timeout |
| `OCR_MAX_SPLIT_PAGES` | `40` | Max split pages |
| `OCR_SEGMENT_PAGES` | `20` | Segment size for long docs |
| `CHANDRA_HF_HOME` / `HF_HOME` | — | Hugging Face cache (HF server / scripts) |

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

### Vector backend (present in some local `.env` files)

These may appear in deployment env for Qdrant/embeddings. Live API code primarily uses **Marqo** via `MARQO_URL` unless a vector-store module is restored.

| Variable | Purpose |
|----------|---------|
| `VECTOR_BACKEND` | Backend selector (`qdrant`, etc.) |
| `QDRANT_URL` | Qdrant base URL |
| `QDRANT_API_KEY` | Qdrant API key |
| `QDRANT_COLLECTION_NAME` | Collection name |
| `QDRANT_TIMEOUT_SECONDS` | Client timeout |
| `EMBEDDING_PROVIDER` | e.g. `sentence_transformers`, `openai_compatible` |
| `EMBEDDING_MODEL` | Embedding model id |
| `EMBEDDING_VECTOR_SIZE` | Vector dimensions |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | Optional remote embeddings API |

---

## Who consumes what

| Concern | FE | API | Worker |
|---------|----|-----|--------|
| `VITE_*` proxies & Keycloak browser login | ✅ | — | — |
| JWT validation (`AUTH_*`, `KEYCLOAK_*`) | — | ✅ | — |
| Temporal / MinIO / SQLite | — | ✅ | ✅ |
| OCR / translation / chunking / domain tags | — | config / status | ✅ runs jobs |
| Marqo URL | proxy only | ✅ | ✅ |

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
