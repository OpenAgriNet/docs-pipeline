# docs Veterinary Books - OCR Pipeline

Temporal-based pipeline with manual review gates at each stage. Supports translation of non-English content (Hindi, Gujarati, etc.) before ingestion.

Supported local input types for `/documents` and `/documents/batch`:
- `.pdf`, `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`, `.jpg`, `.jpeg`, `.png`, `.webp`, `.tif`, `.tiff`
- Non-PDF inputs are converted to PDF before OCR.
- `.csv` and `.xlsx` are ingested via native row parsing (no OCR), which preserves structured tabular content better.

## Metadata Enrichment

During ingestion, chunk records are enriched from optional metadata files:
- `csv/document_manifest.csv` (manifest metadata)
- `search/document_descriptions.jsonl` (LLM descriptions)

Configured via environment variables:
- `DOCUMENT_METADATA_CSV_PATH` (default `/app/csv/document_manifest.csv`)
- `DOCUMENT_DESCRIPTIONS_JSONL_PATH` (default `/app/search/document_descriptions.jsonl`)

Added record fields include:
- `title_en`, `title_gu`, `doc_language`, `category_tags`
- `doc_short_description`, `doc_llm_description`, `ingestion_status`
- `quality_score`, `priority_rank`

The pipeline remains backward-compatible with older Marqo index schemas by dropping unsupported fields automatically at ingest time.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   FastAPI       │────▶│   Temporal      │────▶│   Mistral OCR   │
│   (REST API)    │     │   (Workflows)   │     │   & Translate   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │                       │
        │                       ▼                       │
        │               ┌─────────────────┐             │
        │               │   Lang Detect   │◀────────────┘
        │               │   (Express)     │
        │               └─────────────────┘
        │                       │
        ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│     MinIO       │     │     Marqo       │
│   (Storage)     │     │  (Vector DB)    │
└─────────────────┘     └─────────────────┘
```

## How It Works

### Pipeline Flow (Detailed)

```
                                    ┌─────────────────────────────────────┐
                                    │           PDF Document              │
                                    └──────────────┬──────────────────────┘
                                                   │
                                                   ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  STAGE 1: OCR                                                                        │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐ │
│  │                                                                                 │ │
│  │   PDF ──▶ Base64 Encode ──▶ Mistral OCR API ──▶ Markdown per Page              │ │
│  │                                                                                 │ │
│  │   • Converts PDF pages to base64                                               │ │
│  │   • Sends to Mistral's vision model                                            │ │
│  │   • Returns structured markdown with tables, headers, lists                    │ │
│  │                                                                                 │ │
│  └─────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼
                              ┌────────────────────────────────────┐
                              │  OCR_REVIEW (Manual Gate)          │
                              │                                    │
                              │  User can:                         │
                              │  • View extracted pages            │
                              │  • Edit markdown (fix OCR errors)  │
                              │  • Add reviewer notes              │
                              │  • Reset to original               │
                              │                                    │
                              │  ──▶ POST /approve-ocr to continue │
                              └────────────────────────────────────┘
                                                   │
                                                   ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  STAGE 2: TRANSLATION                                                                │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐ │
│  │                                                                                 │ │
│  │   Pages ──▶ Language Detect ──▶ Translate Non-English ──▶ Pages with           │ │
│  │                    │                    │                 Translations          │ │
│  │                    │                    │                                       │ │
│  │                    ▼                    ▼                                       │ │
│  │              franc-min             Mistral LLM                                  │ │
│  │              detects:              translates:                                  │ │
│  │              • Hindi (hi)          • To English                                 │ │
│  │              • Gujarati (gu)       • Preserves structure                        │ │
│  │              • Marathi (mr)        • Keeps formatting                           │ │
│  │              • English (en)                                                     │ │
│  │                                                                                 │ │
│  └─────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼
                              ┌────────────────────────────────────┐
                              │  TRANSLATION_REVIEW (Manual Gate)  │
                              │                                    │
                              │  User can:                         │
                              │  • View original + translation     │
                              │  • Edit translations               │
                              │  • Add reviewer notes              │
                              │                                    │
                              │  ──▶ POST /approve-translation     │
                              └────────────────────────────────────┘
                                                   │
                                                   ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  STAGE 3: CHUNKING                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐ │
│  │                                                                                 │ │
│  │   Pages ──▶ Combine ──▶ Clean Text ──▶ Split ──▶ Filter ──▶ Chunks             │ │
│  │   (uses translated text if available)                                          │ │
│  │                              │            │          │                          │ │
│  │                              ▼            ▼          ▼                          │ │
│  │                         Remove:      Token-based   Drop chunks                  │ │
│  │                         • HTML tags  ~450 tokens   < 100 tokens                 │ │
│  │                         • LaTeX      128 overlap                                │ │
│  │                         • Artifacts                                             │ │
│  │                                                                                 │ │
│  └─────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼
                              ┌────────────────────────────────────┐
                              │  CHUNK_REVIEW (Manual Gate)        │
                              │                                    │
                              │  User can:                         │
                              │  • View all chunks                 │
                              │  • Edit chunk text                 │
                              │  • Exclude bad chunks              │
                              │  • Add reviewer notes              │
                              │                                    │
                              │  ──▶ POST /approve-chunks          │
                              └────────────────────────────────────┘
                                                   │
                                                   ▼
                              ┌────────────────────────────────────┐
                              │  READY_FOR_INGESTION (Manual Gate) │
                              │                                    │
                              │  Final review before ingestion:    │
                              │  • Verify all chunks are correct   │
                              │  • Check metadata                  │
                              │                                    │
                              │  ──▶ POST /approve-ingestion       │
                              └────────────────────────────────────┘
                                                   │
                                                   ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  STAGE 4: INGESTION                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐ │
│  │                                                                                 │ │
│  │   Chunks ──▶ Prepare Records ──▶ Marqo Index ──▶ Vector Embeddings             │ │
│  │                    │                   │                                        │ │
│  │                    ▼                   ▼                                        │ │
│  │              Add metadata:       Creates embeddings                             │ │
│  │              • doc_id            using multilingual-e5-large                    │ │
│  │              • filename          model for semantic search                      │ │
│  │              • chunk_num                                                        │ │
│  │              • token_count                                                      │ │
│  │                                                                                 │ │
│  └─────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼
                              ┌────────────────────────────────────┐
                              │           COMPLETED                │
                              │                                    │
                              │  Document is now searchable via    │
                              │  Marqo vector search API           │
                              └────────────────────────────────────┘
```

### Workflow State Machine

```
                    ┌──────────────┐
                    │  REGISTERED  │
                    └──────┬───────┘
                           │
                           ▼
                  ┌────────────────┐
                  │ OCR_PROCESSING │
                  └────────┬───────┘
                           │
              ┌────────────┴────────────┐
              │                         │
              ▼                         ▼
     ┌─────────────┐           ┌──────────────┐
     │  OCR_REVIEW │           │    FAILED    │
     │  (waiting)  │           └──────────────┘
     └──────┬──────┘
            │
            │ approve_ocr signal
            ▼
  ┌────────────────────────┐
  │ TRANSLATION_PROCESSING │
  └───────────┬────────────┘
              │
              ▼
    ┌────────────────────┐
    │ TRANSLATION_REVIEW │
    │     (waiting)      │
    └─────────┬──────────┘
              │
              │ approve_translation signal
              ▼
        ┌───────────┐
        │  CHUNKING │
        └─────┬─────┘
              │
              ▼
      ┌──────────────┐
      │ CHUNK_REVIEW │
      │  (waiting)   │
      └──────┬───────┘
             │
             │ approve_chunks signal
             ▼
    ┌─────────────────────┐
    │ READY_FOR_INGESTION │
    │      (waiting)      │
    └──────────┬──────────┘
               │
               │ approve_ingestion signal
               ▼
        ┌───────────┐
        │ INGESTING │
        └─────┬─────┘
              │
              ▼
        ┌───────────┐
        │ COMPLETED │
        └───────────┘
```

### Data Transformation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  INPUT                           PROCESSING                    OUTPUT       │
│  ─────                           ──────────                    ──────       │
│                                                                             │
│  ┌─────────┐                                                                │
│  │   PDF   │                                                                │
│  │ (bytes) │                                                                │
│  └────┬────┘                                                                │
│       │                                                                     │
│       │  OCR Activity                                                       │
│       ▼                                                                     │
│  ┌──────────────────────────────────────────────────────────┐               │
│  │  Pages: [                                                │               │
│  │    {                                                     │               │
│  │      page_number: 1,                                     │               │
│  │      original_markdown: "# Chapter 1\n\nContent...",     │               │
│  │      edited_markdown: null,                              │               │
│  │      is_reviewed: false                                  │               │
│  │    },                                                    │               │
│  │    ...                                                   │               │
│  │  ]                                                       │               │
│  └────────────────────────┬─────────────────────────────────┘               │
│                           │                                                 │
│                           │  Translation Activity                           │
│                           ▼                                                 │
│  ┌──────────────────────────────────────────────────────────┐               │
│  │  Pages (with translations): [                            │               │
│  │    {                                                     │               │
│  │      page_number: 1,                                     │               │
│  │      original_markdown: "# अध्याय 1\n\n...",             │               │
│  │      detected_language: "hi",                            │               │
│  │      translated_markdown: "# Chapter 1\n\n...",          │               │
│  │      edited_translation: null,                           │               │
│  │      translation_reviewed: false                         │               │
│  │    },                                                    │               │
│  │    ...                                                   │               │
│  │  ]                                                       │               │
│  └────────────────────────┬─────────────────────────────────┘               │
│                           │                                                 │
│                           │  Chunking Activity (uses translated text)       │
│                           ▼                                                 │
│  ┌──────────────────────────────────────────────────────────┐               │
│  │  Chunks: [                                               │               │
│  │    {                                                     │               │
│  │      chunk_number: 1,                                    │               │
│  │      original_text: "Chapter 1 content spanning...",     │               │
│  │      token_count: 423,                                   │               │
│  │      page_start: 1,                                      │               │
│  │      page_end: 2,                                        │               │
│  │      is_excluded: false                                  │               │
│  │    },                                                    │               │
│  │    ...                                                   │               │
│  │  ]                                                       │               │
│  └────────────────────────┬─────────────────────────────────┘               │
│                           │                                                 │
│                           │  Ingestion Activity                             │
│                           ▼                                                 │
│  ┌──────────────────────────────────────────────────────────┐               │
│  │  Marqo Records: [                                        │               │
│  │    {                                                     │               │
│  │      _id: "abc123",                                      │               │
│  │      doc_id: "xyz789",                                   │               │
│  │      name: "calf_management",                            │               │
│  │      source: "documents",                          │               │
│  │      chunk_num: 1,                                       │               │
│  │      token_count: 423,                                   │               │
│  │      text: "Chapter 1 content...",  ◄── Vector embedded │               │
│  │    },                                                    │               │
│  │    ...                                                   │               │
│  │  ]                                                       │               │
│  └──────────────────────────────────────────────────────────┘               │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Component Interaction

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│                              USER / CLIENT                                        │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │
                    HTTP REST API       │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI (api.py)                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐                   │
│  │ POST /upload    │  │ GET /documents  │  │ POST /approve-* │                   │
│  │ POST /documents │  │ Query state     │  │ Send signals    │                   │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘                   │
└───────────┼────────────────────┼────────────────────┼─────────────────────────────┘
            │                    │                    │
            │ start_workflow     │ query              │ signal
            ▼                    │                    │
┌───────────────────────┐        │                    │
│    MinIO (:9000)      │        │                    │
│    File Storage       │        │                    │
└───────────────────────┘        │                    │
            │                    ▼                    ▼
            │         ┌────────────────────────────────────────────────────────────┐
            │         │                Temporal Server (:7233)                     │
            │         │  ┌──────────────────────────────────────────────────────┐ │
            │         │  │                   Workflow State                     │ │
            │         │  │  • document_id, filename, filepath                   │ │
            │         │  │  • stage (current state)                             │ │
            │         │  │  • pages[] (OCR + translations)                      │ │
            │         │  │  • chunks[] (chunked text)                           │ │
            │         │  │  • ocr_approved, translation_approved,               │ │
            │         │  │    chunks_approved, ingestion_approved (gate flags)  │ │
            │         │  └──────────────────────────────────────────────────────┘ │
            │         └───────────────────────────┬────────────────────────────────┘
            │                                     │
            │              Task Queue             │ "ocr-pipeline"
            │                                     ▼
            │         ┌────────────────────────────────────────────────────────────┐
            │         │                   Worker (worker.py)                       │
            │         │  ┌─────────────┐  ┌───────────────┐  ┌─────────────────┐  │
            │         │  │  run_ocr()  │  │  translate()  │  │ create_chunks() │  │
            │         │  └──────┬──────┘  └───────┬───────┘  └────────┬────────┘  │
            │         │  ┌──────┴──────┐  ┌───────┴───────┐  ┌────────┴────────┐  │
            │         │  │ingest_marqo │  │ detect_lang() │  │  prep_ingest()  │  │
            │         │  └──────┬──────┘  └───────┬───────┘  └────────┬────────┘  │
            │         └─────────┼─────────────────┼───────────────────┼────────────┘
            │                   │                 │                   │
            ▼                   ▼                 ▼                   ▼
┌───────────────────┐ ┌─────────────────┐ ┌───────────────┐ ┌───────────────────┐
│  Mistral OCR API  │ │ Marqo (:8882)   │ │  Lang Detect  │ │ LangChain Splitter│
│ mistral-ocr-latest│ │ Vector Index    │ │   (:3001)     │ │   + tiktoken      │
└───────────────────┘ └─────────────────┘ └───────────────┘ └───────────────────┘
```

## Workflow Stages

```
REGISTERED → OCR_PROCESSING → OCR_REVIEW → TRANSLATION_PROCESSING → TRANSLATION_REVIEW
                                  ↑                                       ↑
                            [User Review]                           [User Review]
                            [Edit Pages]                          [Edit Translations]
                            [Approve OCR]                        [Approve Translation]

→ CHUNKING → CHUNK_REVIEW → READY_FOR_INGESTION → INGESTING → COMPLETED
                   ↑                  ↑
             [User Review]       [Final Review]
             [Edit Chunks]       [Approve Ingestion]
             [Approve Chunks]
```

## Setup

### Using Docker Compose (Recommended)

```bash
cd /path/to/docs-pipeline

# Start all services (API, Worker, Temporal, MinIO, Marqo)
docker compose up -d

# View logs
docker compose logs -f

# Stop services
docker compose down
```

### Local Development (without Docker)

```bash
cd /path/to/docs-pipeline

# Install Python dependencies
pip install -r requirements.txt

# Set env vars
export MISTRAL_API_KEY=your_key
export TEMPORAL_HOST=localhost:7233
export MARQO_URL=http://localhost:8882
export MINIO_ENDPOINT=localhost:9000
export MINIO_ACCESS_KEY=minioadmin
export MINIO_SECRET_KEY=minioadmin123
```

## Running (Local Development)

### 1. Start Temporal Server

```bash
# Using Temporal CLI (dev mode)
temporal server start-dev

# Or using Docker
docker run -d --name temporal -p 7233:7233 -p 8080:8080 temporalio/auto-setup:latest
```

### 2. Start the Worker

```bash
python -m pipeline.worker
```

### 3. Start the API

```bash
uvicorn pipeline.api:app --reload --port 8001
```

### 4. Start the UI (optional)

```bash
cd ui && npm install && npm run dev
```

API docs: http://localhost:8001/docs
Temporal UI: http://localhost:8080
React UI: http://localhost:3000

## API Documentation

See **[API_DOCS.md](./API_DOCS.md)** for complete REST API reference including:
- Document upload and management
- OCR, Translation, and Chunk review endpoints
- Search settings configuration
- Audit logs
- Marqo direct API

## Quick Start

```bash
# Upload a PDF
curl -X POST "http://localhost:8001/upload" -F "file=@book.pdf"

# List documents
curl "http://localhost:8001/documents"

# Approve stages
curl -X POST "http://localhost:8001/documents/{workflow_id}/approve-ocr"
curl -X POST "http://localhost:8001/documents/{workflow_id}/approve-translation"
curl -X POST "http://localhost:8001/documents/{workflow_id}/approve-chunks"
curl -X POST "http://localhost:8001/documents/{workflow_id}/approve-ingestion"
```

## Workflow Features

| Feature | Description |
|---------|-------------|
| **Durable execution** | Survives crashes, resumes automatically |
| **4 review gates** | OCR, Translation, Chunks, Pre-ingestion |
| **Translation support** | Auto-detects Hindi, Gujarati, Marathi, etc. |
| **Edit at any stage** | Modify pages, translations, or chunks via API |
| **Exclude bad chunks** | Mark chunks to skip during ingestion |
| **Auto-approve mode** | Skip all reviews for batch processing |
| **MinIO storage** | PDF files stored in object storage |
| **SQLite persistence** | Fast document listing during processing |
| **Export anytime** | Get markdown or chunks at any stage |

## Files

```
docs/
├── books/                  # Place supported input files here (PDF/Office/Image)
├── pipeline/
│   ├── __init__.py
│   ├── models.py           # Data models (PageData, ChunkData, DocumentStage)
│   ├── activities.py       # OCR, translation, chunking, ingestion activities
│   ├── workflows.py        # Temporal workflow with signals and queries
│   ├── worker.py           # Temporal worker
│   ├── api.py              # FastAPI REST interface
│   └── db.py               # SQLite state persistence
├── ui/                     # React UI for document review
│   ├── src/App.jsx         # Single-file React app
│   └── package.json
├── lang-detect/            # Language detection microservice
│   ├── src/index.ts        # Express server with franc-min
│   └── package.json
├── docker-compose.yml      # Full stack deployment
├── Dockerfile              # Pipeline API + Worker image
├── requirements.txt        # Python dependencies
└── README.md
```

## Service Ports

| Service | Port | Description |
|---------|------|-------------|
| Pipeline API | 8001 | FastAPI REST API |
| Temporal Server | 7233 | Workflow orchestration |
| Temporal UI | 8080 | Workflow monitoring |
| Marqo | 8882 | Vector search engine |
| MinIO API | 9000 | Object storage |
| MinIO Console | 9001 | Storage web UI |
| Lang Detect | 3001 | Language detection service |
| React UI | 3000 | Document review UI |

## Chunking Settings

| Setting | Default | Description |
|---------|---------|-------------|
| chunk_size | 450 | Max tokens per chunk |
| chunk_overlap | 128 | Overlap between chunks |
| min_tokens | 100 | Minimum chunk size |

Chunking uses LangChain's `RecursiveCharacterTextSplitter` with separators: `\n\n`, `\n`, `.`, ` `

## Marqo Setup

Marqo is the vector search engine for document retrieval.

```bash
# Start Marqo (requires 4GB+ RAM)
docker run -d --name marqo -p 8882:8882 marqoai/marqo:latest
```

### Search Documents

```bash
curl -X POST "http://localhost:8882/indexes/documents-index/search" \
  -H "Content-Type: application/json" \
  -d '{"q": "calf feeding schedule", "limit": 5}'
```

### Index Stats

```bash
curl "http://localhost:8882/indexes/documents-index/stats"
```

## Postman Collection

Import `postman/OCR_Pipeline.postman_collection.json` for easy API testing.

**Variables:**
- `base_url`: Pipeline API (default: `http://127.0.0.1:8001`)
- `marqo_url`: Marqo server (default: `http://127.0.0.1:8882`)
- `workflow_id`: Auto-populated after starting a document

## Book Priority

See `books_priority.md` for categorization of PDFs by indexing priority:
- **HIGH**: India-specific content (NDDB, cooperatives, policies)
- **MEDIUM**: Practical guides with local context
- **LOW**: Public domain / likely in LLM training data
