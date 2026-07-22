#!/usr/bin/env python3
"""Drop-in mock for the Chandra HF OCR HTTP API (local pipeline unblocking).

Implements the same surface as ``scripts/chandra_hf_server.py``:
  GET  /health
  POST /v1/ocr/pages

No GPU, torch, or model download. Returns placeholder markdown for each image
so Temporal ``run_ocr_and_store`` can complete when real Chandra is unavailable.

Usage:
  python scripts/mock_chandra_ocr_server.py
  # or: CHANDRA_HF_PORT=8010 uvicorn ... (see __main__)

Point worker at the same URL as production local config:
  CHANDRA_VLLM_BASE_URL=http://localhost:8010/v1
  CHANDRA_INFERENCE_MODE=hf
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Chandra OCR", version="1.0.0")


class OcrPageRequest(BaseModel):
    images: list[str] = Field(..., description="Base64-encoded PNG/JPEG page images")
    prompt_type: str = "ocr_layout"
    max_output_tokens: int = 12288


class OcrPageResult(BaseModel):
    markdown: str
    error: bool = False


class OcrPageResponse(BaseModel):
    pages: list[OcrPageResult]
    model: str = "mock-chandra"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "backend": "mock"}


@app.post("/v1/ocr/pages", response_model=OcrPageResponse)
def ocr_pages(request: OcrPageRequest) -> OcrPageResponse:
    if not request.images:
        raise HTTPException(status_code=400, detail="images is required")

    pages = [
        OcrPageResult(
            markdown=(
                f"# Mock OCR page {i + 1}\n\n"
                "[mock-chandra] Placeholder text for local development. "
                "Start real Chandra (`scripts/chandra_hf_server.py` or remote vLLM) "
                "for production-quality OCR."
            ),
            error=False,
        )
        for i in range(len(request.images))
    ]
    return OcrPageResponse(pages=pages)


if __name__ == "__main__":
    host = os.environ.get("CHANDRA_HF_HOST", "0.0.0.0")
    port = int(os.environ.get("CHANDRA_HF_PORT", "8010"))
    print(f"Mock Chandra OCR listening on http://{host}:{port} (health: /health)")
    uvicorn.run(app, host=host, port=port)
