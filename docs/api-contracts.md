# Document Ingestion Pipeline — API Contracts

HTTP contracts for the **document ingestion pipeline** control plane
(`pipeline/api.py`). Base URL in local compose is typically
`http://localhost:8001`. The operator UI calls the same routes via same-origin
`/api`.

Interactive OpenAPI is also available from the running API at `/docs`
(Swagger) and `/redoc`.

Flow design: [`ingestion-pipeline-design.md`](ingestion-pipeline-design.md).  
Pydantic models: `pipeline/models.py`.

---

## 1. Conventions

### Auth

| Mode | Behavior |
|---|---|
| `AUTH_DISABLED=true` (default) | No Bearer token required; synthetic local admin |
| Auth enabled | `Authorization: Bearer <access_token>` (Keycloak JWT) |

Permissions used below (when auth is on):

| Permission | Typical roles |
|---|---|
| `upload` | content_curator, admin, master_admin |
| `review` | content_curator, admin, master_admin |
| `pipeline` | content_curator, admin, master_admin |
| `search` | viewer and above |
| `admin` | admin, master_admin |

### Path identity

Almost all document routes use **`workflow_id`** (Temporal / SQLite primary
key), **not** `document_id`. Clients should store `workflow_id` from upload /
register responses.

### Common response: `DocumentSummary`

```json
{
  "document_id": "md5-content-hash",
  "canonical_document_id": "md5-content-hash",
  "workflow_id": "…",
  "filename": "report.pdf",
  "display_name": null,
  "source_filename": "report.pdf",
  "source_manifest_name": null,
  "source_file_fingerprint": "md5-content-hash",
  "authoritative": false,
  "instance": "default",
  "is_demo": false,
  "is_disabled": false,
  "query_enabled": true,
  "enabled_dev": true,
  "enabled_prod": true,
  "stage": "registered",
  "page_count": 0,
  "chunk_count": 0,
  "error_message": null,
  "created_at": "2026-07-22T10:00:00",
  "updated_at": "2026-07-22T10:00:00",
  "reindex_required": false,
  "reindex_reason": null,
  "available_actions": ["approve_ocr"]
}
```

### Stages (`stage` enum)

`registered` · `ocr_processing` · `ocr_review` · `translation_processing` ·
`translation_review` · `chunking` · `chunk_review` · `ready_for_ingestion` ·
`ingesting` · `completed` · `failed`

### Errors

Typical FastAPI errors:

| Status | Meaning |
|---:|---|
| 400 | Bad input / invalid state for action |
| 401 / 403 | Auth / permission / tenant scope |
| 404 | Document or chunk not found |
| 429 | Rate limit (uploads) |
| 502 | Downstream failure (e.g. Marqo delete failed) |

---

## 2. Health

### `GET /health`

No auth.

**Response**

```json
{
  "status": "ok",
  "temporal_connected": true
}
```

### `GET /auth/me`

Current caller (or synthetic admin when auth disabled).

**Response (shape)**

```json
{
  "user_id": "…",
  "username": "…",
  "email": "…",
  "roles": ["master_admin"],
  "permissions": ["upload", "review", "pipeline", "search", "admin"],
  "instances": [],
  "envs": [],
  "auth_disabled": true
}
```

---

## 3. Start ingestion

### `POST /upload`

Upload a file and start `DocumentPipelineWorkflow`.

- **Permission:** `upload`
- **Content-Type:** `multipart/form-data`
- **Rate limit:** upload limiter (default 10/min/IP)

| Field | In | Type | Default | Notes |
|---|---|---|---|---|
| `file` | form | file | required | Allowed extensions (pdf, images, office, sheets, …) |
| `auto_approve` | query | bool | `false` | Skip review gates |
| `stop_after_ocr` | query | bool | `false` | OCR-only run |
| `chunk_size` | query | int | `450` | Chunking hint |
| `chunk_overlap` | query | int | `128` | |
| `min_tokens` | query | int | `100` | |
| `index_name` | query | string | `documents-index` | Target Marqo index name |
| `instance` | query | string | `""` | Tenant; resolved from token / defaults when empty |
| `marqo_url` | query | string | `""` | **Ignored** (server uses `MARQO_URL`) |

**Response:** `DocumentSummary`

---

### `POST /documents`

Register a **server-side filepath** and start the pipeline.

- **Permission:** `upload`
- **Body:**

```json
{ "filepath": "/allowed/path/to/file.pdf" }
```

| Query | Type | Default | Notes |
|---|---|---|---|
| `auto_approve` | bool | `false` | |
| `stop_after_ocr` | bool | `false` | |
| `chunk_size` / `chunk_overlap` / `min_tokens` | int | as above | |
| `index_name` | string | `documents-index` | |
| `instance` | string | `""` | |
| `marqo_url` | string | `""` | Ignored |

Path must be under `ALLOWED_FILE_PATHS`.

**Response:** `DocumentSummary`

---

### `POST /documents/batch`

Batch register multiple files (see OpenAPI for body). Permission: `upload`.

---

## 4. List & inspect documents

### `GET /documents`

- **Permission:** `search` (list scoped by caller instances when auth on)
- **Headers (optional):**
  - `X-Include-Demo: true` — include demo docs
  - `X-Include-Disabled: true` — include soft-deleted docs

**Response:** `DocumentSummary[]`

### `GET /documents/summary` · `GET /documents/cohorts`

Cohort / summary counts for ops dashboards. Permission: `search`.

### `GET /documents/{workflow_id}`

Full detail (`DocumentDetail` = summary + artifacts, jobs, index status,
pages/chunks counts, etc.). Permission: document access + `search`/`review`
as enforced by deps.

### `GET /documents/{workflow_id}/runtime`

Temporal runtime snapshot for the live workflow.

### `GET /documents/{workflow_id}/artifacts`

Artifact metadata list (MinIO-backed).

### `GET /documents/{workflow_id}/artifacts/{artifact_id}/content`

Stream artifact bytes.

### `GET /documents/{workflow_id}/jobs`

Job history for the document.

### `GET /documents/{workflow_id}/stage-io`

Stage input/output inspection payload for ops UI.

### `GET /documents/{workflow_id}/allowed-actions`

Actions the current user may run for this document’s stage.

### `GET /documents/{workflow_id}/graph`

`DocumentGraph` — graph-oriented summary for UI.

### `GET /documents/{workflow_id}/error-details`

Structured error details when `stage=failed`.

### `GET /documents/{workflow_id}/pdf`

Stream source/normalized PDF for preview.

### `GET /pipeline/stages`

Static stage list for steppers (`PIPELINE_STAGES`).

---

## 5. Review gates (approvals)

All approvals signal the running Temporal workflow. Permission: `review`.

| Method | Path | Temporal signal | Typical stage before |
|---|---|---|---|
| `POST` | `/documents/{workflow_id}/approve-ocr` | `approve_ocr` | `ocr_review` |
| `POST` | `/documents/{workflow_id}/approve-translation` | `approve_translation` | `translation_review` |
| `POST` | `/documents/{workflow_id}/approve-chunks` | `approve_chunks` | `chunk_review` |
| `POST` | `/documents/{workflow_id}/approve-ingestion` | `approve_ingestion` | `ready_for_ingestion` |

**Body (optional / shared shape):**

```json
{
  "approved": true,
  "notes": "looks good"
}
```

(`ApprovalRequest` — some handlers may accept empty body.)

**Bulk**

| Method | Path |
|---|---|
| `POST` | `/documents/bulk/approve-ocr` |
| `POST` | `/documents/bulk/approve-translation` |
| `POST` | `/documents/bulk/approve-chunks` |
| `POST` | `/documents/bulk/reindex` |

**Bulk body:**

```json
{
  "workflow_ids": ["wf-1", "wf-2"]
}
```

**Bulk response:**

```json
{
  "results": [
    { "workflow_id": "wf-1", "ok": true, "detail": null },
    { "workflow_id": "wf-2", "ok": false, "detail": "…" }
  ]
}
```

---

## 6. Pages (OCR / translation review)

### `GET /documents/{workflow_id}/pages`

List pages. Permission: `search` / review access.

### `GET /documents/{workflow_id}/pages/{page_num}`

Single page (`page_num` 1-indexed).

### `PATCH /documents/{workflow_id}/pages/{page_num}`

Edit OCR / translation fields. Permission: `review`.

**Body (`PageUpdate`) — all optional**

```json
{
  "edited_markdown": "…",
  "is_reviewed": true,
  "reviewer_notes": "…",
  "edited_translation": "…",
  "translation_reviewed": true,
  "translation_notes": "…"
}
```

### `POST /documents/{workflow_id}/pages/{page_num}/reset`

Reset page edits toward original OCR/translation. Permission: `review`.

### `GET /documents/{workflow_id}/export/markdown`

Export reviewed markdown.

---

## 7. Chunks (chunk review & lifecycle)

### `GET /documents/{workflow_id}/chunks`

| Query | Type | Default |
|---|---|---|
| `include_excluded` | bool | `false` |

### `GET /documents/{workflow_id}/chunks/{chunk_num}`

Single chunk (1-indexed).

### `PATCH /documents/{workflow_id}/chunks/{chunk_num}`

Permission: `review`.

**Body (`ChunkUpdate`) — all optional**

```json
{
  "edited_text": "…",
  "is_reviewed": true,
  "is_excluded": false,
  "reviewer_notes": "…",
  "domain_tags": ["crop:wheat", "topic:irrigation"]
}
```

**Include semantics**

- `is_excluded: true` on a completed document also removes that chunk from Marqo
- Marks reindex / dirty as needed for later republish

### `DELETE /documents/{workflow_id}/chunks/{chunk_num}`

Hard-delete one chunk from SQLite **and** Marqo. Permission: `admin`.

- Chunk numbers are **not** renumbered (gaps remain)
- Reingest will **not** restore the chunk (needs re-chunk)

**Response**

```json
{
  "workflow_id": "…",
  "chunk_number": 3,
  "deleted": true,
  "marqo_deleted": true,
  "chunks_remaining": 11
}
```

### `PUT /documents/{workflow_id}/chunks/{chunk_num}/tags`

Replace manual domain tags.

```json
{ "tags": ["crop:wheat", "region:gujarat"] }
```

### `POST /documents/{workflow_id}/chunks/{chunk_num}/reset`

Reset chunk text/review flags toward original.

### `POST /documents/{workflow_id}/auto-tag-chunks`

Re-run automatic domain tagging for all chunks. Permission: `review`.

### `GET /documents/{workflow_id}/export/chunks`

Export chunks (optional `include_excluded`).

### `GET /chunks/search`

SQLite-first chunk search across documents (maintainer tool). Permission: `search`.

### `GET /taxonomy/domain-tags`

Taxonomy for tag editors.

### `GET /provenance/chunk`

Resolve chunk provenance (query params — see OpenAPI).

---

## 8. Retry, reingest, reconcile

Permission: `pipeline` unless noted.

| Method | Path | Effect |
|---|---|---|
| `POST` | `/documents/{workflow_id}/retry-ocr` | `OcrOnlyWorkflow` |
| `POST` | `/documents/{workflow_id}/retry-translation` | `TranslationOnlyWorkflow` |
| `POST` | `/documents/{workflow_id}/retry-chunking` | `ChunkingOnlyWorkflow` |
| `POST` | `/documents/{workflow_id}/reingest` | `ReingestionWorkflow` — push non-excluded SQLite chunks to Marqo |
| `POST` | `/documents/{workflow_id}/retry-ingestion` | Alias of reingest |
| `POST` | `/documents/{workflow_id}/mark-reindex-required` | Flag dirty for ops |
| `POST` | `/documents/{workflow_id}/clear-reindex-required` | Clear dirty flag |
| `POST` | `/documents/{workflow_id}/reconcile` | Advance SQLite stage to match persisted pages/chunks |
| `POST` | `/documents/reconcile` | Bulk reconcile |

**Reingest response**

```json
{
  "workflow_id": "original-wf-id",
  "reingest_workflow_id": "original-wf-id-reingest-1710000000",
  "chunk_count": 12,
  "status": "started"
}
```

Requires at least one non-excluded chunk in SQLite (else `400`).

---

## 9. Document lifecycle (Include / Delete)

### `POST /documents/{workflow_id}/query-enabled`

Turn document Include on/off for search. Permission: `admin`.

**Body**

```json
{ "query_enabled": false }
```

| `query_enabled` | Effect |
|---|---|
| `false` | Exclude **all** chunks; remove document’s chunks from Marqo; doc stays listed |
| `true` (from off) | Include all chunks again; mark reindex required — **reingest** to republish |

**Response:** `DocumentSummary`

---

### `DELETE /documents/{workflow_id}`

Soft-delete document. Permission: `admin`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `remove_from_search` | bool | `true` | Remove all chunks from Marqo first |

**Effects**

1. Remove from Marqo (fails request with `502` if Marqo delete errors)
2. Cancel running Temporal workflow if possible
3. Set `is_disabled=true`, `query_enabled=false`
4. Mark all chunks excluded

MinIO objects and SQLite history are retained.

**Response**

```json
{
  "workflow_id": "…",
  "disabled": true,
  "workflow_cancelled": false,
  "chunks_excluded": 12,
  "marqo_deleted": 12
}
```

---

### `POST /documents/{workflow_id}/restore`

Clear `is_disabled` only. Chunks stay excluded / out of Marqo until Include +
reingest. Permission: `admin`.

```json
{
  "workflow_id": "…",
  "restored": true,
  "query_enabled": false
}
```

---

### `POST /documents/{workflow_id}/enablement`

Dev/prod enablement matrix (metadata flags). Permission: `admin`.

```json
{
  "enabled_dev": true,
  "enabled_prod": false
}
```

**Response:** `DocumentSummary`

Note: this is separate from Include (`query_enabled`) and soft-delete
(`is_disabled`).

---

### `POST /documents/{workflow_id}/demo`

Mark / unmark demo document (ops). See OpenAPI for body.

---

## 10. Marqo / search (pipeline-adjacent)

### `GET /documents/{workflow_id}/marqo`

Index status vs SQLite for this document.

### `GET /documents/{workflow_id}/marqo/chunks`

Chunks currently present in Marqo for this document.

### `POST /marqo/search`

Search the configured index (settings + filters). Permission: `search`.

Body is a flexible JSON payload (query text, limit, filters, exclude
reference chunks, etc.). Prefer live OpenAPI for the exact accepted keys;
server also merges defaults from search settings.

### `GET /marqo/indexes/summary` · `…/settings` · `…/stats`

Index introspection.

### `GET /admin/index/schema` · `POST /admin/index/create`

Admin index schema / create helpers. Permission: `admin`.

### `GET /admin/ingest-info`

Ingest configuration snapshot for operators.

---

## 11. Audit & settings

### `GET /documents/{workflow_id}/audit` · `GET /audit`

Audit log pages (`AuditLogResponse`).

### `GET /settings/search` · `PUT /settings/search`

Read/update search defaults (`SearchSettings`).

### `GET /settings/search/audit` · `POST /settings/search/reset`

Settings change history / reset to defaults.

---

## 12. Operations queue & runs

### `GET /operations/queue`

Ops work queue (`OperationQueueResponse`).

### `GET /runs` · `GET /runs/{job_id}`

Job/run listing and detail.

---

## 13. Admin users (auth-on deployments)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/admin/users` | List Keycloak users |
| `GET` | `/admin/users/{user_id}` | Detail |
| `PUT` | `/admin/users/{user_id}/access` | Update instances / envs / roles / enabled |

**Access body (`UserAccessUpdate`)**

```json
{
  "instances": ["amul"],
  "envs": ["dev"],
  "roles": ["content_curator"],
  "enabled": true
}
```

Requires Keycloak admin env configuration. Permission: master-admin path.

---

## 14. Quick integration sequence

Typical reviewed ingest from a client:

1. `POST /upload` → save `workflow_id`
2. Poll `GET /documents/{workflow_id}` until `stage=ocr_review`
3. Optionally `PATCH …/pages/{n}` then `POST …/approve-ocr`
4. Wait for `translation_review` → edit/approve
5. Wait for `chunk_review` → edit/Include/tags → `POST …/approve-chunks`
6. Wait for `ready_for_ingestion` → `POST …/approve-ingestion`
7. Wait for `stage=completed`
8. Verify with `GET …/marqo` or `POST /marqo/search`

Trusted backfill: `POST /upload?auto_approve=true` and poll until `completed`.

---

## 15. Related docs

| Doc | Contents |
|---|---|
| [`ingestion-pipeline-design.md`](ingestion-pipeline-design.md) | Stage flow design |
| [`DESIGN.md`](DESIGN.md) | Full architecture |
| [`../README.md`](../README.md) | Runbook / compose |
| Live OpenAPI | `GET /docs` on the API process |
