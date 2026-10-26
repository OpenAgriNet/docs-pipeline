from pathlib import Path
from ops.qdrant_native_source import prepare, FILES


def test_backport_preserves_runtime_schema_and_unrelated_functions(tmp_path):
    runtime=tmp_path/'runtime'; candidate=tmp_path/'candidate'
    runtime.mkdir();candidate.mkdir()
    (runtime/'db.py').write_text('from .vector_store import default_physical_index\n\ndef init_db():\n    return "original schema"\n\ndef ensure_tenant_default_index():\n    return "old"\n')
    (candidate/'db.py').write_text('from .vector_store import default_physical_index, resolve_backend_index\n\ndef init_db():\n    raise RuntimeError("unrelated migration")\n\ndef ensure_tenant_default_index():\n    return "qdrant mapping"\n\ndef get_index_by_backend_name():\n    return "owner"\n\ndef unrelated_feature():\n    return "do not deploy"\n')
    for name in FILES:
        (runtime/name).parent.mkdir(parents=True,exist_ok=True)
        (candidate/name).parent.mkdir(parents=True,exist_ok=True)
        (candidate/name).write_text('# reviewed backend\n')
    manifest=prepare(runtime,candidate)
    text=(runtime/'db.py').read_text()
    assert 'original schema' in text
    assert 'qdrant mapping' in text and 'get_index_by_backend_name' in text
    assert 'unrelated migration' not in text and 'unrelated_feature' not in text
    assert set(manifest)=={'db.py',*FILES}
