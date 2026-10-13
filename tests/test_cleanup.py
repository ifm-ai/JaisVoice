"""The /v1/clean endpoint with an explicitly substituted generation worker, or a mocked remote API."""
import json

from fastapi.testclient import TestClient
import httpx
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


@pytest.fixture
def remote_api(monkeypatch):
    """An OpenAI-compatible API behind httpx's mock transport; records each request."""
    api = {"requests": [], "reply": lambda request: httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "Envoie un message à Sara."}, "finish_reason": "stop"}]})}
    def handler(request):
        api["requests"].append(request)
        return api["reply"](request)
    client = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **options: client(transport=httpx.MockTransport(handler), **options))
    return api


ENDPOINT = {"base_url": "https://api.example.com/v1/", "model": "big-model", "api_key": "sk-test"}


def test_remote_cleanup_sends_the_same_prompt_without_a_local_model(gateway_client, remote_api):
    http, worker = gateway_client
    transcript = "Envoie un message à Ahmed, euh non, à Sara"
    response = http.post("/v1/clean", json={"text": transcript, "endpoint": ENDPOINT})
    assert response.status_code == 200 and response.json()["text"] == "Envoie un message à Sara."
    request = remote_api["requests"][0]
    assert str(request.url) == "https://api.example.com/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "big-model" and body["temperature"] == 0 and body["stream"] is False
    expected = [{"role": message["role"], "content": message["content"]} for message in cleaner_messages(transcript)]
    assert body["messages"] == expected and worker.requests == []


def test_remote_cleanup_drops_leading_reasoning_and_omits_an_empty_key(gateway_client, remote_api):
    http, _ = gateway_client
    remote_api["reply"] = lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "<think>The cue is euh non.</think>\nÀ Sara."}, "finish_reason": "stop"}]})
    response = http.post("/v1/clean", json={"text": "Text", "endpoint": {**ENDPOINT, "api_key": ""}})
    assert response.json()["text"] == "À Sara."
    assert "Authorization" not in remote_api["requests"][0].headers


@pytest.mark.parametrize("status,payload,code,message", [
    (401, {"error": {"message": "Invalid API key"}}, "cleaner_unavailable", "401: Invalid API key"),
    (200, {"choices": [{"message": {"content": "Text"}, "finish_reason": "length"}]}, "cleaner_incomplete", "did not finish"),
    (200, {"choices": [{"message": {"content": " "}, "finish_reason": "stop"}]}, "cleaner_empty", "no corrected text"),
    (200, {"unexpected": True}, "cleaner_unavailable", "unexpected response"),
])
def test_remote_failures_are_explicit(gateway_client, remote_api, status, payload, code, message):
    http, _ = gateway_client
    remote_api["reply"] = lambda request: httpx.Response(status, json=payload)
    detail = http.post("/v1/clean", json={"text": "Text", "endpoint": ENDPOINT}).json()["detail"]
    assert detail["code"] == code and message in detail["message"]


def test_unreachable_api_is_unavailable(gateway_client, remote_api):
    http, _ = gateway_client
    def refuse(request): raise httpx.ConnectError("refused")
    remote_api["reply"] = refuse
    response = http.post("/v1/clean", json={"text": "Text", "endpoint": ENDPOINT})
    assert response.status_code == 502 and "could not be reached" in response.json()["detail"]["message"]


def test_endpoint_must_be_http(gateway_client):
    http, _ = gateway_client
    assert http.post("/v1/clean", json={"text": "Text", "endpoint": {**ENDPOINT, "base_url": "file:///etc"}}).status_code == 422
