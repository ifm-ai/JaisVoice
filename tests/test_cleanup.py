"""The /v1/clean endpoint with an explicitly substituted generation worker."""
from fastapi.testclient import TestClient
import pytest

from jais_runtime import gateway
from jais_runtime.prompts import cleaner_messages


def done(content, finish="stop"):
    return {"finish_reason": finish, "content": content, "usage": {}}


@pytest.fixture
def gateway_client(monkeypatch):
    """The real /v1/clean endpoint with an explicitly substituted generation worker."""
    worker = gateway.Worker()
    worker.outputs, worker.requests = [], []
    def start():
        worker.status, worker.model = "ready", ("3.7B", None, None)
    def fake_generate(job, cancelled, emit):
        worker.requests.append(job)
        emit(("ready", None))
        emit(("done", worker.outputs.pop(0)) if worker.outputs else ("error", "no fixture output"))
        emit(("end", None))
    monkeypatch.setattr(worker, "start", start)
    monkeypatch.setattr(worker, "generate", fake_generate)
    monkeypatch.setattr(gateway, "worker", worker)
    with TestClient(gateway.app) as client:
        yield client, worker


def test_cleaned_text_is_returned(gateway_client):
    http, worker = gateway_client
    transcript = "Envoie un message à Ahmed, euh non, à Sara"
    worker.outputs.append(done("  Envoie un message à Sara.  "))
    response = http.post("/v1/clean", json={"text": transcript, "model_size": "3.7B"})
    assert response.status_code == 200 and response.json()["text"] == "Envoie un message à Sara."
    assert worker.requests[0].context == gateway.CLEANER_CONTEXT and worker.requests[0].tools == []
    assert worker.requests[0].messages == cleaner_messages(transcript)


@pytest.mark.parametrize("finish,content,code", [("length", "Text", "cleaner_incomplete"), ("stop", "  ", "cleaner_empty")])
def test_incomplete_or_empty_output_is_rejected(gateway_client, finish, content, code):
    http, worker = gateway_client
    worker.outputs.append(done(content, finish))
    response = http.post("/v1/clean", json={"text": "Text"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == code


def test_unloaded_size_is_unavailable(gateway_client):
    http, worker = gateway_client
    response = http.post("/v1/clean", json={"text": "Text", "model_size": "0.9B"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "cleaner_unavailable"
    assert worker.requests == []
