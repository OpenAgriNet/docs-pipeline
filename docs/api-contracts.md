# Document Ingestion Pipeline — API Contracts

HTTP contracts for APIs used in the **document ingestion pipeline** control
plane (`pipeline/api.py`). Base URL in local compose is typically
`http://localhost:8001`. The operator UI calls the same routes via same-origin
`/api`.

Interactive OpenAPI is also available from the running API at `/docs`
(Swagger) and `/redoc`.

**Core ingest sections:** §§3–9 and §13 (upload → review → ingest → soft-delete).  
**Adjacent ops:** §§10–12 (search settings, Marqo admin, ops queue).

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
| `manage_users` | admin, master_admin |

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

| Status | Meaning |
|---:|---|
| 400 | Bad input / invalid state for action |
| 401 / 403 | Auth / permission / tenant scope |
| 404 | Document or chunk not found |
| 429 | Rate limit (uploads / batch) |

---

## 2. Health

### `GET /health`

No auth.

```json
{ "status": "ok", "temporal_connected": true }
```

### `GET /auth/me`

Current caller (or synthetic admin when auth disabled).

```json
{
  "user_id": "…",
  "username": "…",
  "email": "…",
  "roles": ["master_admin"],
  "permissions": ["upload", "review", "pipeline", "search", "admin", "manage_users"],
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
| `auto_approve` | query | bool | `false` | Skip review waits |
| `stop_after_ocr` | query | bool | `false` | Temporal ends at `ocr_review` (no approve wait) |
| `chunk_size` | query | int | `450` | Chunking hint |
| `chunk_overlap` | query | int | `128` | |
| `min_tokens` | query | int | `100` | |
| `index_name` | query | string | `documents-index` | Target Marqo index name |
| `instance` | query | string | `""` | Tenant; resolved from token / defaults when empty |
| `marqo_url` | query | string | `""` | **Ignored** (server uses `MARQO_URL`) |

**Response:** `DocumentSummary`

If the same MinIO object path already has a live SQLite row and queryable
Temporal workflow, that existing document is returned (no new run).

---

### `POST /documents`

Register a **server-side filepath** and start the pipeline.

- **Permission:** `upload`
- **Body:**

```json
{ "filepath": "/allowed/path/to/file.pdf" }
```

Same query params as upload (except `file`): `auto_approve`, `stop_after_ocr`,
`chunk_size`, `chunk_overlap`, `min_tokens`, `index_name`, `instance`,
`marqo_url` (ignored).

Path must be under `ALLOWED_FILE_PATHS`.

**Response:** `DocumentSummary`

---

### `POST /documents/batch`

Start a workflow for each supported file in a **server directory**.

- **Permission:** `upload`
- **Rate limit:** `5/minute`
- **Body:**

```json
{ "directory": "/allowed/path/to/folder" }
```

Same query flags as register (`auto_approve`, `stop_after_ocr`, chunk params,
`instance`).

**Response:** `DocumentSummary[]`

---

## 4. List & inspect documents

### `GET /documents`

- **Auth:** any authenticated user (`CurrentUser`); results scoped by caller
  `instances` when auth is on
- **Query:**

| Param | Type | Default |
|---|---|---|
| `stage` | stage enum | omit = all |
| `limit` | int | `100` (max `500`) |
| `offset` | int | `0` |

- **Headers (optional):**
  - `X-Include-Demo: true`
  - `X-Include-Disabled: true`

**Response:** `DocumentSummary[]`

### `GET /documents/summary` · `GET /documents/cohorts`

Cohort / summary counts for ops dashboards. Auth: `CurrentUser`.

### `GET /documents/{workflow_id}`

Full detail (`DocumentDetail`). Auth: document access for caller.

### Other inspect routes

| Method | Path | Notes |
|---|---|---|
| `GET` | `/documents/{workflow_id}/runtime` | Temporal runtime snapshot |
| `GET` | `/documents/{workflow_id}/artifacts` | Artifact metadata list |
| `GET` | `/documents/{workflow_id}/artifacts/{artifact_id}` | Single artifact metadata |
| `GET` | `/documents/{workflow_id}/artifacts/{artifact_id}/content` | Stream bytes |
| `GET` | `/documents/{workflow_id}/jobs` | Job history |
| `GET` | `/documents/{workflow_id}/stage-io` | Stage I/O for ops UI |
| `GET` | `/documents/{workflow_id}/allowed-actions` | Stage actions for caller |
| `GET` | `/documents/{workflow_id}/graph` | `DocumentGraph` |
| `GET` | `/documents/{workflow_id}/error-details` | Failure details |
| `GET` | `/documents/{workflow_id}/pdf` | PDF preview stream |
| `GET` | `/pipeline/stages` | Static `PIPELINE_STAGES` list |

---

## 5. Review gates (approvals)

All single-document approvals **signal** the running Temporal workflow.
Permission: `review`. **No request body.**

| Method | Path | Signal | Expected stage | Response |
|---|---|---|---|---|
| `POST` | `/documents/{workflow_id}/approve-ocr` | `approve_ocr` | `ocr_review` | `{"approved":"ocr","workflow_id":"…"}` |
| `POST` | `/documents/{workflow_id}/approve-translation` | `approve_translation` | `translation_review` | `{"approved":"translation","workflow_id":"…"}` |
| `POST` | `/documents/{workflow_id}/approve-chunks` | `approve_chunks` | `chunk_review` | `{"approved":"chunks","workflow_id":"…"}` |
| `POST` | `/documents/{workflow_id}/approve-ingestion` | `approve_ingestion` | `ready_for_ingestion` | `{"approved":"ingestion","workflow_id":"…"}` |

There is **no** `bulk/approve-ingestion`.

### Bulk

| Method | Path | Permission |
|---|---|---|
| `POST` | `/documents/bulk/approve-ocr` | `review` |
| `POST` | `/documents/bulk/approve-translation` | `review` |
| `POST` | `/documents/bulk/approve-chunks` | `review` |
| `POST` | `/documents/bulk/reindex` | `pipeline` |

**Body (`BulkWorkflowActionRequest`)**

```json
{
  "workflow_ids": ["wf-1", "wf-2"],
  "dry_run": false
}
```

**Response (`BulkWorkflowActionResponse`)**

```json
{
  "action": "approve_ocr",
  "dry_run": false,
  "requested": 2,
  "succeeded": 1,
  "failed": 1,
  "results": [
    { "workflow_id": "wf-1", "ok": true, "action": "approve_ocr", "message": "…" },
    { "workflow_id": "wf-2", "ok": false, "action": "approve_ocr", "message": "…" }
  ]
}
```

---

## 6. Pages (OCR / translation review)

### `GET /documents/{workflow_id}/pages`

List pages.

### `GET /documents/{workflow_id}/pages/{page_num}`

Single page (`page_num` 1-indexed).

### `PATCH /documents/{workflow_id}/pages/{page_num}`

Permission: `review`.

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

## 7. Chunks (chunk review)

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

**Exclude semantics**

- Setting `is_excluded: true` when the document `stage` is `completed` also
  removes that chunk from Marqo
- Marks reindex / dirty as needed for later republish

### `PUT /documents/{workflow_id}/chunks/{chunk_num}/tags`

```json
{ "tags": ["crop:wheat", "region:gujarat"] }
```

### `POST /documents/{workflow_id}/chunks/{chunk_num}/reset`

Reset chunk text/review flags toward original.

### `POST /documents/{workflow_id}/auto-tag-chunks`

Re-run automatic domain tagging. Permission: `review`.

### `GET /documents/{workflow_id}/export/chunks`

Export chunks (optional `include_excluded`).

### `GET /chunks/search`

SQLite-first chunk search across documents. Permission: `search`.

### `GET /taxonomy/domain-tags`

Taxonomy for tag editors.

### `GET /provenance/chunk`

Resolve chunk provenance (query params — see OpenAPI).

---

## 8. Retry, reingest, reconcile

Permission: `pipeline` unless noted.

| Method | Path | Effect / notes |
|---|---|---|
| `POST` | `/documents/{workflow_id}/retry-ocr` | Starts `OcrOnlyWorkflow` |
| `POST` | `/documents/{workflow_id}/retry-translation` | Starts `TranslationOnlyWorkflow` |
| `POST` | `/documents/{workflow_id}/retry-chunking` | Starts `ChunkingOnlyWorkflow`; query: `chunk_size`, `chunk_overlap`, `min_tokens` |
| `POST` | `/documents/{workflow_id}/reingest` | Starts `ReingestionWorkflow`; query: `index_name`, `marqo_url` (ignored) |
| `POST` | `/documents/{workflow_id}/retry-ingestion` | Alias of reingest |
| `POST` | `/documents/{workflow_id}/mark-reindex-required` | Body: `{ "reason": "…" }` optional |
| `POST` | `/documents/{workflow_id}/clear-reindex-required` | Clears dirty flag |
| `POST` | `/documents/{workflow_id}/reconcile` | Materialized + Temporal sync |
| `POST` | `/documents/reconcile` | Bulk reconcile |

**Typical retry response**

```json
{
  "workflow_id": "original-wf-id",
  "status": "started",
  "retry_workflow_id": "original-wf-id-retry-ocr-1710000000"
}
```

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

## 9. Document soft-delete / restore / demo

### `DELETE /documents/{workflow_id}`

Soft-delete document. Permission: `admin`.

| Query | Type | Default | Notes |
|---|---|---|---|
| `remove_from_search` | bool | `true` | Remove all chunks from Marqo |

**Effects**

1. Cancel running Temporal workflow if possible
2. Set `is_disabled=true` in SQLite
3. Optionally remove chunks from Marqo

MinIO objects and SQLite history are retained.

**Response**

```json
{
  "workflow_id": "…",
  "disabled": true,
  "workflow_cancelled": false,
  "marqo_deleted": 12
}
```

---

### `POST /documents/{workflow_id}/restore`

Clear `is_disabled` only. Does **not** automatically re-index Marqo — use
reingest. Permission: `admin`.

```json
{
  "workflow_id": "…",
  "restored": true
}
```

---

### `POST /documents/{workflow_id}/demo`

Mark / unmark demo document. Permission: `admin`.

| Query | Type | Default |
|---|---|---|
| `is_demo` | bool | `true` |

```json
{ "workflow_id": "…", "is_demo": true }
```

---

## 10. Marqo / search (adjacent)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/documents/{workflow_id}/marqo` | Index status vs SQLite |
| `GET` | `/documents/{workflow_id}/marqo/chunks` | Chunks present in Marqo |
| `POST` | `/marqo/search` | Search index; permission `search` |
| `GET` | `/marqo/indexes/summary` · `…/settings` · `…/stats` | Index introspection |
| `GET` | `/admin/index/schema` | Admin schema check |
| `POST` | `/admin/index/create` | Admin create helper |
| `GET` | `/admin/ingest-info` | Ingest config snapshot |

Prefer live OpenAPI for `POST /marqo/search` body keys; server merges search
settings defaults.

---

## 11. Audit & settings (adjacent)

| Method | Path | Permission |
|---|---|---|
| `GET` | `/documents/{workflow_id}/audit` · `/audit` | as enforced by route deps |
| `GET` | `/settings/search` | `search` |
| `PUT` | `/settings/search` | `admin` |
| `GET` | `/settings/search/audit` | `admin` |
| `POST` | `/settings/search/reset` | `admin` |

---

## 12. Operations queue & runs (adjacent)

| Method | Path |
|---|---|
| `GET` | `/operations/queue` |
| `GET` | `/runs` |
| `GET` | `/runs/{job_id}` |

---

## 13. Quick integration sequence

Typical reviewed ingest from a client:

1. `POST /upload` → save `workflow_id`
2. Poll `GET /documents/{workflow_id}` until `stage=ocr_review`
3. Optionally `PATCH …/pages/{n}` then `POST …/approve-ocr` (empty body)
4. Wait for `translation_review` → edit/approve
5. Wait for `chunk_review` → edit/exclude/tags → `POST …/approve-chunks`
6. Wait for `ready_for_ingestion` → `POST …/approve-ingestion`
7. Wait for `stage=completed`
8. Verify with `GET …/marqo` or `POST /marqo/search`

Trusted backfill: `POST /upload?auto_approve=true` and poll until `completed`.

---

## 14. Related docs

| Doc | Contents |
|---|---|
| [`ingestion-pipeline-design.md`](ingestion-pipeline-design.md) | Stage flow design |
| [`DESIGN.md`](DESIGN.md) | Full architecture |
| [`../README.md`](../README.md) | Runbook / compose |
| Live OpenAPI | `GET /docs` on the API process |
