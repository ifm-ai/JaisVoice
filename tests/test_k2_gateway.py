"""HTTP framing tests with an explicitly substituted generation worker."""
import json
import threading
from fastapi.testclient import TestClient
import pytest
from k2_runtime import gateway


@pytest.fixture
def client(monkeypatch):
    test_worker = gateway.Worker()
    test_worker.status = 'ready'
    test_worker.models = {'k2-cleaner': (None, None, {}, {'test_fixture': True})}
    monkeypatch.setattr(test_worker, 'load', lambda: None)
    monkeypatch.setattr(gateway, 'worker', test_worker)
    with TestClient(gateway.app) as client:
        yield client, test_worker


def test_streamed_error_is_explicit_not_success(client, monkeypatch):
    http, worker = client
    def fake_run(body, cancelled, emit):
        emit(('ready', {'prompt_tokens': 7}))
        emit(('error', 'K2 generation reached its token limit before completing'))
        emit(('end', None))
    monkeypatch.setattr(worker, 'run', fake_run)
    response = http.post('/v1/chat/completions', json={'model': 'k2-cleaner', 'messages': [{'role': 'user', 'content': 'test fixture'}], 'stream': True})
    assert response.status_code == 200
    assert 'incomplete_generation' in response.text
    assert '[DONE]' not in response.text
    assert worker.pending == 0
    assert not worker.active_flags


def test_nonstream_failure_does_not_return_clean_text(client, monkeypatch):
    http, worker = client
    def fake_run(body, cancelled, emit):
        emit(('ready', {'prompt_tokens': 7}))
        emit(('delta', {'content': 'incomplete'}))
        emit(('error', 'incomplete tool-call markup'))
        emit(('end', None))
    monkeypatch.setattr(worker, 'run', fake_run)
    response = http.post('/v1/chat/completions', json={'model': 'k2-cleaner', 'messages': [{'role': 'user', 'content': 'test fixture'}]})
    assert response.status_code == 422
    assert 'choices' not in response.json()
    assert worker.pending == 0


def test_queued_cancel_does_not_enter_model_generation():
    worker = gateway.Worker()
    flag = threading.Event()
    flag.set()
    emitted = []
    worker.run(None, flag, emitted.append)
    assert emitted == [('end', None)]
    worker.executor.shutdown()
