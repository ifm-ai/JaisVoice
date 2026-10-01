import sys
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from k2_runtime import gateway


@pytest.fixture
def memory_worker(monkeypatch):
    w = gateway.Worker()
    w.status = 'ready'
    w._model_health = {
        alias: {'display_name': size, 'path': '/fixture/'+size, 'context_limit': limit,
                'reasoning_efforts': ['low'], 'status': 'ready'}
        for alias, size, limit in [('k2-cleaner', '1B', 4096), ('k2-agent', '4B', 16384)]
    }
    w.models = {alias: (object(), object(), {'max_position_embeddings': 32768}, dict(entry))
                for alias, entry in w._model_health.items()}
    fake_core = ModuleType('mlx.core')
    fake_core.clear_cache = lambda: None
    fake_core.eval = lambda _: None
    fake_core.array = lambda x: x
    fake_mlx = ModuleType('mlx'); fake_mlx.__path__ = []; fake_mlx.core = fake_core
    fake_model = ModuleType('k2_runtime.model')
    fake_model.load_model = lambda _: (lambda x: x, object(), {'max_position_embeddings': 32768})
    for name, module in [('mlx', fake_mlx), ('mlx.core', fake_core), ('k2_runtime.model', fake_model)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(gateway, 'prepare_k2_prompt', lambda *args: None)
    monkeypatch.setattr(gateway, 'worker', w)
    monkeypatch.setattr(w, 'load', lambda: None)
    with TestClient(gateway.app) as client:
        yield client, w, fake_model


def test_unload_is_independent_and_remaining_model_serves_both_roles(memory_worker):
    http, w, _ = memory_worker
    remaining = w.models['k2-cleaner'][0]
    result = http.post('/models/4B/memory', json={'action': 'unload'})
    assert result.status_code == 200
    assert result.json()['models']['k2-agent']['status'] == 'unloaded'
    assert result.json()['models']['k2-cleaner']['status'] == 'ready'
    assert w.models['k2-cleaner'][0] is remaining
    for role in gateway.MODEL_ALIASES:
        body = gateway.CompletionRequest(model=role, model_size='1B', messages=[{'role':'user','content':'hi'}])
        assert w.resolve_model(body)[0] is remaining
    assert http.post('/v1/chat/completions', json={'model':'k2-agent','model_size':'4B','messages':[{'role':'user','content':'hi'}]}).status_code == 409
    assert http.get('/v1/models').json()['data'][0]['id'] == 'k2-cleaner'
    assert len(http.get('/v1/models').json()['data']) == 1
    assert http.post('/models/4B/memory', json={'action':'load'}).json()['models']['k2-agent']['status'] == 'ready'
    assert list(w.models) == ['k2-agent']
    assert w._model_health['k2-cleaner']['status'] == 'unloaded'


def test_both_can_be_unloaded_then_independently_reloaded(memory_worker):
    http, w, _ = memory_worker
    for size in ['1B','4B']:
        assert http.post(f'/models/{size}/memory',json={'action':'unload'}).status_code == 200
    assert not w.models
    assert w.status == 'ready'  # service remains available for reload
    assert http.post('/models/1B/memory',json={'action':'load'}).status_code == 200
    assert list(w.models) == ['k2-cleaner']
    assert w._model_health['k2-agent']['status'] == 'unloaded'


@pytest.mark.parametrize('busy', ['pending','lifecycle_busy','active_request'])
def test_memory_changes_reject_busy_worker(memory_worker, busy):
    http, w, _ = memory_worker
    setattr(w, busy, 1)
    assert http.post('/models/1B/memory',json={'action':'unload'}).status_code == 409
    assert len(w.models) == 2


def test_reload_failure_keeps_other_model_unloaded(memory_worker):
    http, w, loader = memory_worker
    http.post('/models/4B/memory',json={'action':'unload'})
    def fail(_): raise ValueError('test missing weights')
    loader.load_model = fail
    assert http.post('/models/4B/memory',json={'action':'load'}).status_code == 500
    assert w._model_health['k2-agent']['status'] == 'error'
    assert w._model_health['k2-cleaner']['status'] == 'unloaded'
    assert not w.lifecycle_busy


@pytest.mark.parametrize('size,alias', [('1B','k2-cleaner'), ('4B','k2-agent')])
def test_load_evicts_other_model_even_when_target_already_loaded(memory_worker, size, alias):
    http, worker, _ = memory_worker
    assert http.post(f'/models/{size}/memory', json={'action':'load'}).status_code == 200
    assert list(worker.models) == [alias]

