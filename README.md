# docs Veterinary Books - OCR Pipeline

Temporal-based pipeline with manual review gates at each stage.

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   FastAPI       │────▶│   Temporal      │────▶│   Mistral OCR   │
│   (REST API)    │     │   (Workflows)   │     │                 │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │
        │                       ▼
        │               ┌─────────────────┐
        └──────────────▶│     Marqo       │
                        └─────────────────┘
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
│  STAGE 2: CHUNKING                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐ │
│  │                                                                                 │ │
│  │   Pages ──▶ Combine ──▶ Clean Text ──▶ Split ──▶ Filter ──▶ Chunks             │ │
│  │                              │            │          │                          │ │
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
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  STAGE 3: INGESTION                                                                  │
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
  └──────────┬──────────┘
             │
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
│                           │  Chunking Activity                              │
│                           ▼                                                 │
│  ┌──────────────────────────────────────────────────────────┐               │
│  │  Chunks: [                                               │               │
│  │    {                                                     │               │
│  │      chunk_number: 1,                                    │               │
│  │      original_text: "Chapter 1 content spanning...",     │               │
│  │      token_count: 423,                                   │               │
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
│  │ POST /documents │  │ GET /documents  │  │ POST /approve-* │                   │
│  │ Start workflow  │  │ Query state     │  │ Send signals    │                   │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘                   │
└───────────┼────────────────────┼────────────────────┼─────────────────────────────┘
            │                    │                    │
            │ start_workflow     │ query              │ signal
            ▼                    ▼                    ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                           Temporal Server (:7233)                                 │
│  ┌─────────────────────────────────────────────────────────────────────────────┐ │
│  │                         Workflow State                                      │ │
│  │  • document_id, filename, filepath                                          │ │
│  │  • stage (current state)                                                    │ │
│  │  • pages[] (OCR results)                                                    │ │
│  │  • chunks[] (chunked text)                                                  │ │
│  │  • ocr_approved, chunks_approved (gate flags)                               │ │
│  └─────────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │
                    Task Queue          │ "ocr-pipeline"
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                              Worker (worker.py)                                   │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐                   │
│  │   run_ocr()     │  │ create_chunks() │  │ ingest_to_marqo │                   │
│  │   Activity      │  │   Activity      │  │   Activity      │                   │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘                   │
└───────────┼────────────────────┼────────────────────┼─────────────────────────────┘
            │                    │                    │
            ▼                    │                    ▼
┌───────────────────────┐        │        ┌───────────────────────┐
│    Mistral OCR API    │        │        │    Marqo (:8882)      │
│  mistral-ocr-latest   │        │        │  Vector Search Index  │
└───────────────────────┘        │        └───────────────────────┘
                                 │
                                 ▼
                    ┌───────────────────────┐
                    │   LangChain Splitter  │
                    │   + tiktoken          │
                    └───────────────────────┘
```

## Workflow Stages

```
REGISTERED → OCR_PROCESSING → OCR_REVIEW → CHUNKING → CHUNK_REVIEW → INGESTING → COMPLETED
                                  ↑                        ↑
                            [User Review]            [User Review]
                            [Edit Pages]             [Edit Chunks]
                            [Approve]                [Approve]
```

## Setup

```bash
cd /path/to/docs-pipeline

# Install dependencies
pip install temporalio fastapi uvicorn mistralai tiktoken langchain-text-splitters marqo

# Set env vars
export MISTRAL_API_KEY=your_key
export TEMPORAL_HOST=localhost:7233  # Optional, defaults to localhost:7233
```

## Running

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
uvicorn pipeline.api:app --reload --port 8000
```

API docs: http://localhost:8000/docs
Temporal UI: http://localhost:8080

## API Usage

### Register Documents

```bash
# Single document
curl -X POST "http://localhost:8000/documents" \
  -H "Content-Type: application/json" \
  -d '{"filepath": "/path/to/book.pdf"}'

# Batch (all PDFs in folder)
curl -X POST "http://localhost:8000/documents/batch" \
  -H "Content-Type: application/json" \
  -d '{"directory": "./books"}'

# Auto-approve (skip manual review)
curl -X POST "http://localhost:8000/documents?auto_approve=true" \
  -H "Content-Type: application/json" \
  -d '{"filepath": "/path/to/book.pdf"}'
```

### List Documents

```bash
# All documents
curl "http://localhost:8000/documents"

# Filter by stage
curl "http://localhost:8000/documents?stage=ocr_review"
```

### Review OCR (Pages)

```bash
# Get all pages
curl "http://localhost:8000/documents/{workflow_id}/pages"

# Get single page
curl "http://localhost:8000/documents/{workflow_id}/pages/1"

# Edit page markdown
curl -X PATCH "http://localhost:8000/documents/{workflow_id}/pages/1" \
  -H "Content-Type: application/json" \
  -d '{"edited_markdown": "# Fixed heading\n\nCorrected text..."}'

# Mark page as reviewed
curl -X PATCH "http://localhost:8000/documents/{workflow_id}/pages/1" \
  -H "Content-Type: application/json" \
  -d '{"is_reviewed": true}'

# Reset to original
curl -X POST "http://localhost:8000/documents/{workflow_id}/pages/1/reset"

# Approve OCR (continue to chunking)
curl -X POST "http://localhost:8000/documents/{workflow_id}/approve-ocr"
```

### Review Chunks

```bash
# Get all chunks
curl "http://localhost:8000/documents/{workflow_id}/chunks"

# Edit chunk text
curl -X PATCH "http://localhost:8000/documents/{workflow_id}/chunks/1" \
  -H "Content-Type: application/json" \
  -d '{"edited_text": "Corrected chunk text..."}'

# Exclude bad chunk
curl -X PATCH "http://localhost:8000/documents/{workflow_id}/chunks/5" \
  -H "Content-Type: application/json" \
  -d '{"is_excluded": true}'

# Approve chunks (continue to ingestion)
curl -X POST "http://localhost:8000/documents/{workflow_id}/approve-chunks"
```

### Export

```bash
# Export as markdown
curl "http://localhost:8000/documents/{workflow_id}/export/markdown"

# Export chunks for Marqo
curl "http://localhost:8000/documents/{workflow_id}/export/chunks"
```

## Workflow Features

| Feature | Description |
|---------|-------------|
| **Durable execution** | Survives crashes, resumes automatically |
| **Manual review gates** | Workflow pauses for user approval |
| **Edit at any stage** | Modify pages or chunks via API |
| **Exclude bad chunks** | Mark chunks to skip during ingestion |
| **Auto-approve mode** | Skip review for batch processing |
| **Export anytime** | Get markdown or chunks at any stage |

## Files

```
docs/
├── books/                  # Place PDFs here
├── pipeline/
│   ├── __init__.py
│   ├── models.py           # Data models
│   ├── activities.py       # OCR, chunking, ingestion
│   ├── workflows.py        # Temporal workflow with signals
│   ├── worker.py           # Temporal worker
│   └── api.py              # FastAPI REST interface
└── README.md
```

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

# Or use remote server (update URL in pipeline/activities.py)
# Current: http://127.0.0.1:8882
```

### Search Documents

```bash
curl -X POST "http://127.0.0.1:8882/indexes/documents-index/search" \
  -H "Content-Type: application/json" \
  -d '{"q": "calf feeding schedule", "limit": 5}'
```

### Index Stats

```bash
curl "http://127.0.0.1:8882/indexes/documents-index/stats"
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
