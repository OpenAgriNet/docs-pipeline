import json

import pytest

from ops.qdrant_mirror_health import mirror_health


def status(tmp_path, **overrides):
    path = tmp_path / "status.json"
    data = {"ok": True, "collection": "owned", "last_success_at": 100,
            "payloads_verified": 3, "source_points": 3, "target_points": 3}
    data.update(overrides)
    path.write_text(json.dumps(data))
    return path


def test_fresh_exact_reconciliation_is_healthy(tmp_path):
    assert mirror_health(status(tmp_path), "owned", now=110)["age_seconds"] == 10


@pytest.mark.parametrize("changes", [{"ok": False}, {"last_success_at": 0}, {"collection": "other"},
                                     {"payloads_verified": 2}, {"target_points": 2}])
def test_failed_stale_or_unrelated_report_is_rejected(tmp_path, changes):
    with pytest.raises(RuntimeError):
        mirror_health(status(tmp_path, **changes), "owned", now=200)


def test_missing_status_is_not_healthy(tmp_path):
    with pytest.raises(RuntimeError, match="unavailable"):
        mirror_health(tmp_path / "missing.json", "owned", now=110)
