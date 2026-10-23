"""Every app, router, and service module must import independently.

This covers the complete app -> routers -> services graph as first imports.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

MODULES = [
    "pipeline.app",
    "pipeline.routers",
    "pipeline.routers.admin",
    "pipeline.routers.content",
    "pipeline.routers.documents",
    "pipeline.routers.documents_actions",
    "pipeline.routers.search",
    "pipeline.routers.tenants",
    "pipeline.services",
    "pipeline.services.access",
    "pipeline.services.documents",
    "pipeline.services.indexes",
    "pipeline.services.search",
    "pipeline.services.source_files",
    "pipeline.services.taxonomy",
    "pipeline.services.tenants",
    "pipeline.services.workflow_runtime",
    "pipeline.ingestion_records",
    "pipeline.storage",
    "pipeline.storage.minio",
    "pipeline.temporal",
    "pipeline.temporal.client",
    "pipeline.temporal.document_tasks",
    "pipeline.temporal.document_workflows",
    "pipeline.temporal.failures",
    "pipeline.temporal.registry",
    "pipeline.temporal.worker",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports_cleanly_as_first_pipeline_import(module_name):
    """Catch import-order cycles hidden by modules cached earlier in pytest."""
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
