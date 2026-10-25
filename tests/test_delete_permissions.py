import pytest

from pipeline.api import _delete_denial_reason
from pipeline.auth.jwt import claims_to_user


def _user(sub, group):
    return claims_to_user({"sub": sub, "groups": [group]})


def _doc(stage, uploader="other", **extra):
    return {"instance": "up", "stage": stage, "uploaded_by_user_id": uploader, **extra}


SUPER = _user("super", "/global/super-admin")
ADMIN = _user("admin", "/states/UP/admin")
APPROVER = _user("approver", "/states/UP/approver")
CONTRIBUTOR = _user("contrib", "/states/UP/contributor")
VIEW = _user("view", "/states/UP/view")


def test_super_admin_can_delete_and_purge():
    assert _delete_denial_reason(_doc("completed"), SUPER) is None
    assert _delete_denial_reason(_doc("completed"), SUPER, purge=True) is None


@pytest.mark.parametrize("stage", ["registered", "ocr_review", "completed"])
def test_state_admin_can_delete_any_document_in_state(stage):
    assert _delete_denial_reason(_doc(stage), ADMIN) is None


def test_state_admin_cannot_purge():
    assert _delete_denial_reason(_doc("completed"), ADMIN, purge=True) is not None


def test_state_admin_of_other_state_cannot_delete():
    assert _delete_denial_reason(_doc("completed"), _user("mh", "/states/MH/admin")) is not None


@pytest.mark.parametrize("user", [APPROVER, VIEW])
def test_approver_and_viewer_cannot_delete(user):
    assert _delete_denial_reason(_doc("registered", uploader=user.user_id), user) is not None


@pytest.mark.parametrize("stage", ["registered", "ocr_processing"])
def test_contributor_can_delete_own_upload_before_review(stage):
    assert _delete_denial_reason(_doc(stage, uploader="contrib"), CONTRIBUTOR) is None


def test_contributor_can_delete_own_upload_that_failed_before_review():
    assert _delete_denial_reason(_doc("failed", uploader="contrib"), CONTRIBUTOR) is None


@pytest.mark.parametrize("stage", ["ocr_review", "translation_review", "completed"])
def test_contributor_cannot_delete_after_review(stage):
    assert _delete_denial_reason(_doc(stage, uploader="contrib"), CONTRIBUTOR) is not None


def test_contributor_cannot_delete_others_upload():
    assert _delete_denial_reason(_doc("registered"), CONTRIBUTOR) is not None
