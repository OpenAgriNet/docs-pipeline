"""Source guards: ingest errors must not parse nginx HTML as JSON.

The UI has no JS test harness. These fail if New Document goes back to
``response.json()`` on upload, which turned 413/502 HTML into
``Unexpected token '<'``.
"""

from pathlib import Path

INGEST_VIEW = Path(__file__).resolve().parents[1] / "ui" / "src" / "views" / "NewDocumentView.jsx"
PIPELINE_UI = Path(__file__).resolve().parents[1] / "ui" / "src" / "lib" / "pipelineUi.js"


def test_ingest_upload_uses_fetchjson_not_raw_json():
    source = INGEST_VIEW.read_text(encoding="utf-8")
    assert "fetchJson(`/upload?" in source
    assert "response.json()" not in source
    assert "apiFetch" not in source


def test_fetchjson_maps_html_gateway_and_size_errors():
    source = PIPELINE_UI.read_text(encoding="utf-8")
    assert "export function httpStatusHint" in source
    assert "export function formatHttpError" in source
    assert "HTTP 413" in source
    assert "HTTP 500" in source
    assert "HTTP ${status}" in source or "HTTP 502" in source
    assert "content-type" in source
