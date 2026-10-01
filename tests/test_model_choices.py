from fastapi.testclient import TestClient
import pytest

from k2_runtime import gateway
from k2_runtime.gateway import CompletionRequest, Worker
from model_fixtures import loaded


@pytest.mark.parametrize("role", ["k2-cleaner", "k2-agent"])
@pytest.mark.parametrize("size", ["1B", "4B", "7B"])
def test_size_selection_uses_role_context_and_checkpoint_framing(role, size):
    worker = Worker()
    try:
        worker.models = loaded(["1B", "4B", "7B"])
        worker.models["7B"][3]["tool_call_format"] = "xml_typed"
        body = CompletionRequest(model=role, model_size=size, messages=[{"role": "user", "content": "test"}])
        chosen = worker.resolve_model(body)
        assert chosen[:3] == worker.models[size][:3]
        assert chosen[3]["context_limit"] == (4096 if role == "k2-cleaner" else 16384)
        assert chosen[3].get("tool_call_format") == worker.models[size][3].get("tool_call_format")
        assert worker.models[size][3]["context_limit"] == 16384
        del worker.models[size]
        with pytest.raises(ValueError, match="not available"):
            worker.resolve_model(body)
    finally:
        worker.executor.shutdown()


def test_checkpoint_context_limit_caps_the_role_budget():
    worker = Worker()
    try:
        worker.models = loaded(["4B"], max_positions=8192)
        body = CompletionRequest(model="k2-agent", messages=[{"role": "user", "content": "x"}])
        assert worker.resolve_model(body)[3]["context_limit"] == 8192
        worker.models.update(loaded(["1B"]))
        with pytest.raises(ValueError, match="Specify"):
            worker.resolve_model(body)
    finally:
        worker.executor.shutdown()


@pytest.fixture
def recording_gateway(monkeypatch):
    worker = Worker()
    worker.status = "ready"
    worker.models = loaded(["1B", "4B", "7B"])
    worker.requests = []
    def fake_run(body, cancelled, emit):
        worker.requests.append((body.model, body.model_size, body.reasoning_effort))
        emit(("ready", {"prompt_tokens": 1}))
        emit(("done", {"finish_reason": "stop", "content": "Bonjour", "reasoning_content": "", "tool_calls": [], "usage": {}, "metrics": {}}))
        emit(("end", None))
    monkeypatch.setattr(worker, "load", lambda: None)
    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(gateway, "worker", worker)
    with TestClient(gateway.app) as client:
        yield client, worker


@pytest.mark.parametrize("size", ["1B", "4B", "7B"])
def test_cleaner_size_is_request_scoped(recording_gateway, size):
    http, worker = recording_gateway
    worker._model_health = {size: {"cleaner_reasoning_effort": "medium", "agent_reasoning_effort": "low"}}
    assert http.post("/v1/clean", json={"text": "Bonjour", "model_size": size}).json()["text"] == "Bonjour"
    assert worker.requests == [("k2-cleaner", size, "medium")]
    # Several loaded models and no size: refused rather than guessed.
    assert http.post("/v1/clean", json={"text": "Bonjour"}).status_code == 409
    assert http.post("/v1/clean", json={"text": "Bonjour", "model_size": "13B"}).status_code == 422
    assert len(worker.requests) == 1


def test_pi_model_ids_select_the_agent_role_and_size(recording_gateway):
    http, worker = recording_gateway
    for model in ("k2-1b", "k2-4b", "k2-7b"):
        response = http.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "Bonjour"}]})
        assert response.status_code == 200 and response.json()["model"] == "k2-agent"
    assert [request[:2] for request in worker.requests] == [("k2-agent", "1B"), ("k2-agent", "4B"), ("k2-agent", "7B")]
    assert http.post("/v1/chat/completions", json={"model": "k2-13b", "messages": [{"role": "user", "content": "x"}]}).status_code == 404


@pytest.mark.parametrize("size", ["1B", "4B"])
def test_cleanup_and_agent_share_identical_weight_and_tokenizer_instances(size):
    worker = Worker()
    try:
        worker.models = loaded(["1B", "4B"])
        requests = [CompletionRequest(model=role, model_size=size, messages=[{"role": "user", "content": "test"}]) for role in ("k2-cleaner", "k2-agent")]
        cleaner, agent = [worker.resolve_model(request) for request in requests]
        assert cleaner[0] is agent[0] and cleaner[1] is agent[1] and cleaner[2] is agent[2]
        assert cleaner[3]["context_limit"] == 4096
        assert agent[3]["context_limit"] == 16384
    finally:
        worker.executor.shutdown()
