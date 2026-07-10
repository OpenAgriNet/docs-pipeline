#!/usr/bin/env python3
"""Local Chandra OCR 2 HTTP server using the HuggingFace backend (GPU required)."""

from __future__ import annotations

import base64
import io
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from PIL import Image

app = FastAPI(title="Chandra HF OCR", version="1.0.0")
_manager = None


class OcrPageRequest(BaseModel):
    images: list[str] = Field(..., description="Base64-encoded PNG/JPEG page images")
    prompt_type: str = "ocr_layout"
    max_output_tokens: int = 12384


class OcrPageResult(BaseModel):
    markdown: str
    error: bool = False


class OcrPageResponse(BaseModel):
    pages: list[OcrPageResult]
    model: str = "chandra-2-hf"


def _load_manager():
    global _manager
    if _manager is not None:
        return _manager

    os.environ.setdefault(
        "HF_HOME",
        os.environ.get("CHANDRA_HF_HOME", os.path.expanduser("~/.cache/huggingface")),
    )
    os.environ.setdefault("HF_HUB_CACHE", os.path.join(os.environ["HF_HOME"], "hub"))

    from chandra.model import InferenceManager

    _manager = InferenceManager(method="hf")
    return _manager


@app.on_event("startup")
def startup() -> None:
    _load_manager()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "backend": "hf"}


@app.post("/v1/ocr/pages", response_model=OcrPageResponse)
def ocr_pages(request: OcrPageRequest) -> OcrPageResponse:
    if not request.images:
        raise HTTPException(status_code=400, detail="images is required")

    try:
        from chandra.model.schema import BatchInputItem
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="chandra-ocr is not installed") from exc

    manager = _load_manager()
    batch = []
    for image_b64 in request.images:
        raw = base64.b64decode(image_b64)
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        batch.append(BatchInputItem(image=image, prompt_type=request.prompt_type))

    results = manager.generate(batch, max_output_tokens=request.max_output_tokens)
    pages = [
        OcrPageResult(markdown=(item.markdown or ""), error=bool(item.error))
        for item in results
    ]
    return OcrPageResponse(pages=pages)


if __name__ == "__main__":
    host = os.environ.get("CHANDRA_HF_HOST", "0.0.0.0")
    port = int(os.environ.get("CHANDRA_HF_PORT", "8010"))
    uvicorn.run(app, host=host, port=port)
