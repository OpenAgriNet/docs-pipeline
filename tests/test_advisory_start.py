"""Tests for starting advisory workflows (one per record)."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from pipeline.advisory_start import start_advisory_index, start_advisory_promote
from pipeline.workflows import AdvisoryIndexWorkflow, AdvisoryPromoteWorkflow


def _started_ids(client):
    return [call.kwargs["id"] for call in client.start_workflow.await_args_list]


def test_index_starts_one_workflow_per_record():
    client = AsyncMock()
    started = asyncio.run(start_advisory_index(client, ["a", "b"]))
    assert started == 2
    assert _started_ids(client) == ["advisory-a", "advisory-b"]
    first = client.start_workflow.await_args_list[0]
    assert first.args[0] is AdvisoryIndexWorkflow.run
    assert first.kwargs["args"] == ["a"]
    assert first.kwargs["task_queue"] == "ocr-pipeline"


def test_promote_uses_separate_ids():
    client = AsyncMock()
    assert asyncio.run(start_advisory_promote(client, ["a"])) == 1
    assert _started_ids(client) == ["advisory-promote-a"]
    assert client.start_workflow.await_args_list[0].args[0] is AdvisoryPromoteWorkflow.run


def test_already_running_workflow_is_skipped_and_others_still_start():
    client = AsyncMock()
    client.start_workflow.side_effect = [
        WorkflowAlreadyStartedError("advisory-a", "AdvisoryIndexWorkflow"),
        None,
    ]
    assert asyncio.run(start_advisory_index(client, ["a", "b"])) == 1


def test_other_errors_are_logged_and_do_not_stop_the_loop():
    client = AsyncMock()
    client.start_workflow.side_effect = [RuntimeError("temporal down"), None]
    assert asyncio.run(start_advisory_index(client, ["a", "b"])) == 1


def test_no_ids_starts_nothing():
    client = AsyncMock()
    assert asyncio.run(start_advisory_index(client, [])) == 0
    client.start_workflow.assert_not_called()


@pytest.mark.db
class TestBackfillOnStart:
    def _seed(self, db):
        db.upsert_webhook_records(
            "webhook",
            [{"contentID": 1, "status": "Published", "isPublished": 1, "popList": []}],
        )

    def test_off_by_default(self, db_connection, monkeypatch):
        from pipeline.advisory_start import backfill_on_start

        monkeypatch.delenv("ADVISORY_BACKFILL_ON_START", raising=False)
        self._seed(db_connection)
        client = AsyncMock()
        assert asyncio.run(backfill_on_start(client)) == 0
        client.start_workflow.assert_not_called()

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
    def test_on_queues_all_unindexed_rows(self, db_connection, monkeypatch, value):
        from pipeline.advisory_start import backfill_on_start

        monkeypatch.setenv("ADVISORY_BACKFILL_ON_START", value)
        self._seed(db_connection)
        client = AsyncMock()
        assert asyncio.run(backfill_on_start(client)) == 1
        assert _started_ids(client)[0].startswith("advisory-")

    def test_on_with_nothing_unindexed_starts_nothing(self, db_connection, monkeypatch):
        from pipeline.advisory_start import backfill_on_start

        monkeypatch.setenv("ADVISORY_BACKFILL_ON_START", "true")
        client = AsyncMock()
        assert asyncio.run(backfill_on_start(client)) == 0
        client.start_workflow.assert_not_called()

    def test_off_values(self, db_connection, monkeypatch):
        from pipeline.advisory_start import backfill_on_start

        self._seed(db_connection)
        for value in ("false", "0", "", "no"):
            monkeypatch.setenv("ADVISORY_BACKFILL_ON_START", value)
            client = AsyncMock()
            assert asyncio.run(backfill_on_start(client)) == 0
