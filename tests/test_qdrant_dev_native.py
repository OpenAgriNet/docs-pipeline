"""Validate the deployed service's native-writer boundary without loading CUDA."""
import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict, ValidationError


@pytest.fixture
def service(monkeypatch, tmp_path):
    import os
    path = Path(__file__).parents[1] / 'ops' / 'qdrant_dev_search.py'
    tree = ast.parse(path.read_text())
    wanted = {'require_current_source', 'Embeddings', 'embeddings'}
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in wanted]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(os=os, json=json, Path=Path, HTTPException=HTTPException,
                     BaseModel=BaseModel, Field=Field, ConfigDict=ConfigDict,
                     COLLECTION='final', mirror_health=Mock(return_value={'mode': 'mirror'}),
                     lock=threading.Lock(), encoder=SimpleNamespace(
                         dense_model_name='e5', _dense=SimpleNamespace(embed=Mock(return_value=[[1., 2.]]))))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    monkeypatch.setenv('QDRANT_INGESTION_MODE', 'native')
    marker = tmp_path / 'native.json'
    marker.write_text(json.dumps(dict(collection='final', backend='qdrant', mirror_disabled=True)))
    monkeypatch.setenv('QDRANT_NATIVE_MARKER_PATH', str(marker))
    return namespace, marker


def test_native_search_does_not_depend_on_marqo_mirror(service):
    ns, _ = service
    assert ns['require_current_source']()['mode'] == 'native'
    ns['mirror_health'].assert_not_called()


@pytest.mark.parametrize('change', [{'collection': 'wrong'}, {'backend': 'marqo'}, {'mirror_disabled': False}])
def test_native_marker_must_match_verified_cutover(service, change):
    ns, marker = service
    d = json.loads(marker.read_text()); d.update(change); marker.write_text(json.dumps(d))
    with pytest.raises(HTTPException) as error:
        ns['require_current_source']()
    assert error.value.status_code == 503


def test_native_missing_marker_is_unavailable(service):
    ns, marker = service
    marker.unlink()
    with pytest.raises(HTTPException):
        ns['require_current_source']()


def test_mirror_default_keeps_existing_freshness_check(service, monkeypatch):
    ns, _ = service
    monkeypatch.delenv('QDRANT_INGESTION_MODE')
    monkeypatch.setenv('QDRANT_SYNC_STATUS_PATH', '/status')
    ns['require_current_source']()
    ns['mirror_health'].assert_called_once_with('/status', 'final')


def test_embeddings_preserve_prefix_and_raw_values(service):
    ns, _ = service
    result = ns['embeddings'](ns['Embeddings'](model='e5', input=['passage: cow health']))
    assert result['data'][0]['embedding'] == [1., 2.]
    ns['encoder']._dense.embed.assert_called_once_with(['passage: cow health'], batch_size=16)


@pytest.mark.parametrize('model,text', [('wrong', 'hello'), ('e5', ' ')])
def test_invalid_embeddings_never_invoke_gpu(service, model, text):
    ns, _ = service
    with pytest.raises(HTTPException):
        ns['embeddings'](ns['Embeddings'](model=model, input=[text]))
    ns['encoder']._dense.embed.assert_not_called()
