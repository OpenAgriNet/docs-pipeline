import pytest
from unittest.mock import AsyncMock


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_translation_forwards_force_flag(monkeypatch):
    from pipeline.auth.models import local_bypass_user
    from pipeline.routers import documents_actions

    started = {}
    logged = {}

    monkeypatch.setattr(
        documents_actions.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "instance": "default",
        },
    )
    monkeypatch.setattr(documents_actions.db, "get_pages", lambda workflow_id: [{"page_number": 1}])
    monkeypatch.setattr(
        documents_actions.workflow_runtime,
        "start_translation_retry",
        AsyncMock(side_effect=lambda **kwargs: started.update(kwargs)),
    )
    monkeypatch.setattr(documents_actions.db, "create_document_job", lambda **kwargs: 123)
    monkeypatch.setattr(documents_actions.db, "update_document_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        documents_actions.db,
        "log_audit",
        lambda **kwargs: logged.update(kwargs),
    )

    result = await documents_actions.retry_translation(
        "wf-1",
        local_bypass_user(),
        force_retranslate=True,
    )

    assert started["args"] == ["wf-1", "doc-1", "doc.pdf", True]
    assert result["force_retranslate"] is True
    assert logged["metadata"]["force_retranslate"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_translation_defaults_force_false(monkeypatch):
    from pipeline.auth.models import local_bypass_user
    from pipeline.routers import documents_actions

    started = {}

    monkeypatch.setattr(
        documents_actions.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "instance": "default",
        },
    )
    monkeypatch.setattr(documents_actions.db, "get_pages", lambda workflow_id: [{"page_number": 1}])
    monkeypatch.setattr(
        documents_actions.workflow_runtime,
        "start_translation_retry",
        AsyncMock(side_effect=lambda **kwargs: started.update(kwargs)),
    )
    monkeypatch.setattr(documents_actions.db, "create_document_job", lambda **kwargs: 123)
    monkeypatch.setattr(documents_actions.db, "update_document_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(documents_actions.db, "log_audit", lambda **kwargs: None)

    result = await documents_actions.retry_translation("wf-2", local_bypass_user())

    assert started["args"] == ["wf-2", "doc-1", "doc.pdf", False]
    assert result["force_retranslate"] is False
