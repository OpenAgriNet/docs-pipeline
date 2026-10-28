import hashlib
from types import SimpleNamespace
import pytest
from ops.final_corpus_backfill import validate_manifest


def test_backfill_manifest_requires_verified_missing_source(tmp_path):
    (tmp_path/'originals').mkdir()
    data=b'original bytes'
    (tmp_path/'originals'/'one.pdf').write_bytes(data)
    row={'filename':'one.pdf','original_final_points':'0','download_status':'original_verified',
         'downloaded_md5':hashlib.md5(data).hexdigest()}
    validate_manifest([row],tmp_path)
    with pytest.raises(ValueError,match='not a missing-target'):
        validate_manifest([{**row,'original_final_points':'1'}],tmp_path)
    with pytest.raises(ValueError,match='fingerprint mismatch'):
        validate_manifest([{**row,'downloaded_md5':'wrong'}],tmp_path)
    with pytest.raises(ValueError,match='Unsafe or repeated'):
        validate_manifest([row,row],tmp_path)
