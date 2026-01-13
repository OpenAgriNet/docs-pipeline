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
