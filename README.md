# Document Ingestion Pipeline

This repository contains a review-driven document ingestion pipeline built around Temporal workflows, FastAPI, SQLite, MinIO, and Marqo. It is designed for teams that need to normalize heterogeneous files, extract structured text, review and correct outputs, generate chunks, and publish searchable records into a vector index.

The system is intentionally operational, not just algorithmic. Documents move through explicit stages, every major output can be persisted as an artifact, and the operator UI is designed to inspect and manage the pipeline rather than hide it.

## What This System Does

At a high level, the pipeline supports:

- ingestion of document files, images, office documents, and spreadsheets
- normalization into a canonical processing form
- OCR and extraction
- optional translation for non-English content
- chunk generation
- manual review and correction of pages, translations, and chunks
- indexing of approved chunks into Marqo
- operational inspection of workflow, artifacts, audit history, and index state

The system is suitable for:

- knowledge base ingestion
- multilingual document processing
- regulated or review-heavy ingestion workflows
- search and retrieval pipelines that need provenance and operator controls

## Core Architecture

The platform is composed of six main services:

- `api`
  - FastAPI application exposing ingestion, review, artifact, search, and admin endpoints
- `worker`
  - Temporal worker running OCR, translation, chunking, ingestion, and state-update activities
- `temporal`
  - workflow orchestration and retry engine
- `minio`
  - object storage for original uploads, normalized files, and stage artifacts
- `marqo`
  - vector and lexical search index for approved chunks
- `ui`
  - React operator console for dashboard, document review, search workbench, settings, and audit

Supporting service:

- `lang-detect`
  - lightweight language detection service used before translation

## Conceptual Pipeline Stages

The pipeline is stage-based. Each stage has a purpose, a persistent state transition, and a corresponding operator surface.

### 1. Registered

The document has been accepted into the system and assigned a workflow identifier.

Typical outputs:

- document row in SQLite
- original file reference
- initial job record

### 2. OCR Processing

The source file is normalized if needed and passed through OCR or native structured extraction.

Typical behavior:

- PDFs and office/image inputs are normalized toward a document-processing form
- CSV and XLSX inputs can be parsed without OCR
- OCR output is produced page by page conceptually, then persisted into the document state

### 3. OCR Review

Operators inspect extracted page content and correct OCR mistakes before downstream processing continues.

This is where the system becomes review-driven instead of fully automatic.

### 4. Translation Processing

Non-English content is translated into a target language for downstream chunking and search.

The current implementation keeps translation provider and model metadata so translation outputs remain attributable.

### 5. Translation Review

Operators review machine translation before chunking. This is important in multilingual or domain-heavy corpora where terminology needs supervision.

### 6. Chunking

Reviewed page content is transformed into chunks suitable for search and retrieval.

Each chunk is expected to remain traceable to:

- document
- page start
- page end
- chunk order
- run configuration

### 7. Chunk Review

Operators can inspect, edit, exclude, and eventually tag chunks before ingestion.

This is also the right stage for future chunk tagging and reindex-dirty tracking.

### 8. Ready For Ingestion

Final gate before indexing approved chunks.

### 9. Ingesting

Approved chunks are written into Marqo using a passage-style schema.

### 10. Completed

The document is fully processed and indexed.

### 11. Failed

The workflow encountered a non-recoverable failure or exceeded retry limits.

## Data Model

The system is organized around a few core entity types.

### Documents

Top-level business objects representing an ingested source.

### Jobs

Discrete runs such as ingestion, OCR-only, translation-only, chunking, or reingestion operations.

### Artifacts

Persisted files or exports associated with a document and stage, such as:

- original uploads
- normalized files
- OCR JSON exports
- translation JSON exports
- chunk exports
- Marqo payload exports

### Pages

OCR output and page-level review state.

### Chunks

Chunked text, review state, exclusion state, and page-span lineage.

### Index Status

Document-level view of what has been pushed to Marqo.

## Storage Responsibilities

### SQLite

SQLite is the canonical metadata and review-state store.

It owns:

- document rows
- page rows
- chunk rows
- jobs
- artifact metadata
- audit logs
- search settings
- document/index status

### MinIO

MinIO stores document and stage artifacts.

Typical artifact types include:

- original uploads
- normalized PDF or spreadsheet outputs
- OCR page exports
- translation exports
- chunk exports
- Marqo payload snapshots

### Temporal

Temporal is the orchestration layer.

It is responsible for:

- retries
- workflow lifecycle
- review gates
- long-running task resilience

It is not the canonical store for edited content.

### Marqo

Marqo is the search-facing index.

It should be treated as a downstream projection of approved chunk state, not as the source of truth for content editing.

## Supported Inputs

The pipeline supports these input classes:

- PDF documents
- images:
  - `.jpg`
  - `.jpeg`
  - `.png`
  - `.webp`
  - `.tif`
  - `.tiff`
- office documents:
  - `.doc`
  - `.docx`
  - `.ppt`
  - `.pptx`
  - `.xls`
  - `.xlsx`
- delimited and spreadsheet data:
  - `.csv`
  - `.xlsx`

General behavior:

- document-like inputs are normalized toward PDF processing
- spreadsheet inputs can remain spreadsheet-oriented and skip OCR when native parsing is better

## Repository Layout

```text
pipeline/        FastAPI app, Temporal workflows, activities, models, database logic
ui/              React operator console
lang-detect/     Language detection microservice
scripts/         Operational and maintenance scripts
docs/            Supporting design and operational notes
tests/           Automated tests
test_data/       Small local fixtures for tests and smoke checks
docker-compose.yml
Dockerfile
requirements.txt
```

## Services And Ports

Default local ports from `docker-compose.yml`:

- UI: `3000`
- API: `8001`
- Marqo: `8882`
- Temporal: `7233`
- Temporal UI: `8080`
- MinIO API: `9000`
- MinIO console: `9001`

## Running The Stack

### Prerequisites

- Docker and Docker Compose
- an OCR provider API key exposed as `MISTRAL_API_KEY`
- enough local disk for SQLite, MinIO artifacts, and Marqo state

Optional but recommended:

- GPU runtime support if using a GPU-backed Marqo image

### Start

```bash
docker compose up -d --build
```

### Stop

```bash
docker compose down
```

### Health Checks

Useful endpoints after startup:

```bash
curl http://localhost:8001/health
curl http://localhost:8882/
curl http://localhost:9000/minio/health/live
```

## Environment Variables

Important runtime variables include:

- `MISTRAL_API_KEY`
- `TEMPORAL_HOST`
- `MARQO_URL`
- `MINIO_ENDPOINT`
- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`
- `MINIO_BUCKET`
- `DOCUMENT_DB_PATH`
- `LANG_DETECT_URL`
- `TRANSLATION_PROVIDER`
- `TRANSLATION_MODEL`
- `TRANSLATION_PAGE_CONCURRENCY`
- `TRANSLATION_MAX_RETRIES`
- `TRANSLATION_RETRY_BASE_SECONDS`
- `TEMPORAL_MAX_CONCURRENT_ACTIVITIES`
- `DOCUMENT_METADATA_CSV_PATH`
- `DOCUMENT_DESCRIPTIONS_JSONL_PATH`
- `CORS_ORIGINS`
- `ALLOWED_FILE_PATHS`

Optional metadata files:

- `DOCUMENT_METADATA_CSV_PATH`
  - optional manifest-style metadata enrichment file
- `DOCUMENT_DESCRIPTIONS_JSONL_PATH`
  - optional per-document descriptions enrichment file

If these files are not present, the pipeline still works; metadata enrichment is simply reduced.

## Hosting Model

The simplest production-style deployment pattern is:

- expose the UI at a public hostname
- expose the API either:
  - behind the same domain under `/api`, or
  - at a separate internal hostname behind a reverse proxy
- keep Temporal, MinIO, and Marqo internal to the deployment network

Recommended routing shape:

- `https://your-ui-host/` -> UI
- `https://your-ui-host/api/` -> API

This keeps browser calls same-origin and avoids hardcoded environment-specific domains in the frontend.

## Operator UI

The UI is an operations console, not just an upload form.

Current views include:

- dashboard
- new document
- document operations
- search workbench
- settings
- audit log

The document operations screen is intended to expose:

- current stage
- stage runtime
- artifacts
- jobs
- pages
- translations
- chunks
- Marqo state
- audit history

## API Overview

The API is organized around a few major groups.

### Ingestion

- `POST /documents`
- `POST /upload`
- `POST /documents/batch`

### Document Listing And Summary

- `GET /documents`
- `GET /documents/summary`
- `GET /documents/{workflow_id}`
- `GET /documents/{workflow_id}/runtime`
- `GET /documents/{workflow_id}/jobs`
- `GET /documents/{workflow_id}/stage-io`

### Page Review

- `GET /documents/{workflow_id}/pages`
- `PATCH /documents/{workflow_id}/pages/{page_number}`
- `POST /documents/{workflow_id}/pages/{page_number}/reset`

### Translation Review

Translation data is surfaced through the page model and review endpoints.

### Chunk Review

- `GET /documents/{workflow_id}/chunks`
- `PATCH /documents/{workflow_id}/chunks/{chunk_number}`
- `POST /documents/{workflow_id}/chunks/{chunk_number}/reset`

### Approval Gates

- `POST /documents/{workflow_id}/approve-ocr`
- `POST /documents/{workflow_id}/approve-translation`
- `POST /documents/{workflow_id}/approve-chunks`
- `POST /documents/{workflow_id}/approve-ingestion`

### Artifacts

- `GET /documents/{workflow_id}/artifacts`
- `GET /documents/{workflow_id}/artifacts/{artifact_id}`
- `GET /documents/{workflow_id}/artifacts/{artifact_id}/content`

### Index And Search

- `GET /documents/{workflow_id}/marqo`
- `GET /documents/{workflow_id}/marqo/chunks`
- `POST /documents/{workflow_id}/reingest`
- `POST /marqo/search`
- `GET /marqo/indexes/{index_name}/settings`
- `GET /marqo/indexes/{index_name}/stats`

### Audit And Settings

- audit endpoints
- search runtime settings endpoints

The easiest way to inspect the complete surface is to run the API and open the generated OpenAPI docs at:

```text
http://localhost:8001/docs
```

## Search Model

The search workbench and API support a configurable Marqo retrieval surface, including:

- hybrid, tensor, or lexical modes
- candidate pool sizing
- final result limits
- hybrid alpha
- RRF tuning
- optional query expansion profile
- optional E5 query prefixing
- rerank mode selection
- per-document result diversity

The default example index name in this showcase branch is:

- `documents-index`

## Scripts

The `scripts/` directory contains operational helpers for:

- inspecting Marqo fields
- counting indexed records
- resetting or creating indexes
- bulk reingesting SQLite chunks into Marqo
- listing failed workflows
- terminating stuck workflows

These are intended as operator tools, not hidden one-off commands.

## Tests

Run the test suite with:

```bash
pytest
```

Useful quick checks:

```bash
python3 -m py_compile pipeline/*.py
cd ui && npm run build
```

## Design Notes

This repository favors explicitness over silent automation:

- review stages are visible
- artifacts are first-class
- runtime state is inspectable
- downstream indexing is a separate concern from content ownership

That makes it a good fit for teams that need document traceability and operational control rather than a black-box ingestion flow.
