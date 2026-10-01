import sys
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from k2_runtime import gateway
from model_fixtures import inventory


@pytest.fixture
def memory_worker(monkeypatch, tmp_path):
    w = gateway.Worker()
    w.status = 'ready'
    installed = inventory(tmp_path, ("1B", "4B", "7B"))
    installed["7B"]["installed"] = False
    monkeypatch.setattr(gateway, 'read_inventory', lambda: {size: dict(entry) for size, entry in installed.items()})
    w.models = {size: (object(), object(), {'max_position_embeddings': 32768}, dict(installed[size])) for size in ("1B", "4B")}
    w.refresh_inventory()
    fake_core = ModuleType('mlx.core')
    fake_core.clear_cache = lambda: None
    fake_core.eval = lambda _: None
    fake_core.array = lambda x: x
    fake_core.get_active_memory = lambda: 0
    fake_mlx = ModuleType('mlx'); fake_mlx.__path__ = []; fake_mlx.core = fake_core
    fake_model = ModuleType('k2_runtime.model')
    fake_model.load_model = lambda _: (lambda x: x, object(), {'max_position_embeddings': 32768})
    for name, module in [('mlx', fake_mlx), ('mlx.core', fake_core), ('k2_runtime.model', fake_model)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(gateway, 'prepare_k2_prompt', lambda *args: None)
    monkeypatch.setattr(gateway, 'worker', w)
    monkeypatch.setattr(w, 'load', lambda: None)
    with TestClient(gateway.app) as client:
        yield client, w, fake_model, installed


def statuses(response):
    return {size: entry['status'] for size, entry in response.json()['models'].items()}


def test_unload_is_independent_and_remaining_model_serves_both_roles(memory_worker):
    http, w, _, _ = memory_worker
    remaining = w.models['1B'][0]
    result = http.post('/models/4B/memory', json={'action': 'unload'})
    assert result.status_code == 200
    assert statuses(result) == {'1B': 'ready', '4B': 'unloaded', '7B': 'not_installed'}
    for role in gateway.MODEL_ROLES:
        body = gateway.CompletionRequest(model=role, model_size='1B', messages=[{'role': 'user', 'content': 'hi'}])
        assert w.resolve_model(body)[0] is remaining
    assert http.post('/v1/chat/completions', json={'model': 'k2-agent', 'model_size': '4B', 'messages': [{'role': 'user', 'content': 'hi'}]}).status_code == 409
    assert [model['id'] for model in http.get('/v1/models').json()['data']] == ['k2-1b']
    assert statuses(http.post('/models/4B/memory', json={'action': 'load'}))['4B'] == 'ready'
    assert list(w.models) == ['4B']
    assert w._model_health['1B']['status'] == 'unloaded'


def test_both_can_be_unloaded_then_independently_reloaded(memory_worker):
    http, w, _, _ = memory_worker
    for size in ['1B', '4B']:
        assert http.post(f'/models/{size}/memory', json={'action': 'unload'}).status_code == 200
    assert not w.models
    assert w.status == 'ready'  # service remains available for reload
    assert http.post('/models/1B/memory', json={'action': 'load'}).status_code == 200
    assert list(w.models) == ['1B']
    assert w._model_health['4B']['status'] == 'unloaded'


def test_missing_model_cannot_be_loaded_and_unknown_size_is_rejected(memory_worker):
    http, w, _, _ = memory_worker
    response = http.post('/models/7B/memory', json={'action': 'load'})
    assert response.status_code == 500 and 'not installed' in response.json()['detail']
    assert w._model_health['7B']['status'] == 'not_installed'
    assert http.post('/models/13B/memory', json={'action': 'load'}).status_code == 422
    assert set(w.models) == {'1B', '4B'}


def test_deleted_model_is_reported_and_downloaded_model_becomes_loadable(memory_worker):
    http, w, _, installed = memory_worker
    http.post('/models/1B/memory', json={'action': 'unload'})
    installed['1B']['installed'] = False
    installed['7B']['installed'] = True
    assert statuses(http.get('/health')) == {'1B': 'not_installed', '4B': 'ready', '7B': 'unloaded'}
    assert http.post('/models/7B/memory', json={'action': 'load'}).status_code == 200
    assert list(w.models) == ['7B']


@pytest.mark.parametrize('busy', ['pending', 'lifecycle_busy', 'active_request'])
def test_memory_changes_reject_busy_worker(memory_worker, busy):
    http, w, _, _ = memory_worker
    setattr(w, busy, 1)
    assert http.post('/models/1B/memory', json={'action': 'unload'}).status_code == 409
    assert len(w.models) == 2


def test_reload_failure_keeps_other_model_unloaded(memory_worker):
    http, w, loader, _ = memory_worker
    http.post('/models/4B/memory', json={'action': 'unload'})
    def fail(_): raise ValueError('test missing weights')
    loader.load_model = fail
    assert http.post('/models/4B/memory', json={'action': 'load'}).status_code == 500
    assert w._model_health['4B']['status'] == 'error'
    assert http.get('/health').json()['models']['4B']['error'] == 'test missing weights'
    assert w._model_health['1B']['status'] == 'unloaded'
    assert not w.lifecycle_busy


@pytest.mark.parametrize('size', ['1B', '4B'])
def test_load_evicts_other_model_even_when_target_already_loaded(memory_worker, size):
    http, worker, _, _ = memory_worker
    assert http.post(f'/models/{size}/memory', json={'action': 'load'}).status_code == 200
    assert list(worker.models) == [size]
