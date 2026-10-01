from fastapi.testclient import TestClient
import pytest

from k2_runtime import gateway
from k2_runtime.gateway import CompletionRequest, Worker


@pytest.mark.parametrize("role", ["k2-cleaner", "k2-agent"])
@pytest.mark.parametrize("size", ["1B", "4B"])
def test_checkpoint_selection_preserves_role_context_and_checkpoint_framing(role, size):
    worker = Worker()
    try:
        worker.models = {
            "k2-cleaner": (object(), object(), {"max_position_embeddings": 131072},
                           {"display_name": "MBZUAI · 1B", "context_limit": 4096, "tool_call_format": "xml"}),
            "k2-agent": (object(), object(), {"max_position_embeddings": 32768},
                         {"display_name": "MBZUAI · 4B", "context_limit": 16384, "tool_call_format": "xml_typed"}),
        }
        body = CompletionRequest(model=role, model_size=size, messages=[{"role": "user", "content": "test"}])
        chosen = worker.resolve_model(body)
        source = worker.models["k2-cleaner" if size == "1B" else "k2-agent"]
        assert chosen[:3] == source[:3]
        assert chosen[3]["context_limit"] == (4096 if role == "k2-cleaner" else 16384)
        assert chosen[3]["tool_call_format"] == source[3]["tool_call_format"]
        assert worker.models["k2-cleaner"][3]["context_limit"] == 4096
        del worker.models["k2-agent" if size == "1B" else "k2-cleaner"]
        missing = "4B" if size == "1B" else "1B"
        with pytest.raises(ValueError, match="pas disponible"):
            worker.resolve_model(body.model_copy(update={"model": next(iter(worker.models)), "model_size": missing}))
    finally:
        worker.executor.shutdown()


@pytest.fixture
def recording_gateway(monkeypatch):
    worker = Worker()
    worker.status = "ready"
    worker.models = {"k2-cleaner": (None, None, {}, {}), "k2-agent": (None, None, {}, {})}
    worker.requests = []
    def fake_run(body, cancelled, emit):
        worker.requests.append((body.model, body.model_size))
        emit(("ready", {"prompt_tokens": 1}))
        emit(("done", {"finish_reason": "stop", "content": "Bonjour", "reasoning_content": "", "tool_calls": [], "usage": {}, "metrics": {}}))
        emit(("end", None))
    monkeypatch.setattr(worker, "load", lambda: None)
    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(gateway, "worker", worker)
    with TestClient(gateway.app) as client:
        yield client, worker


@pytest.mark.parametrize("size", ["1B", "4B"])
def test_cleaner_size_is_request_scoped(recording_gateway, size):
    http, worker = recording_gateway
    assert http.post("/v1/clean", json={"text": "Bonjour", "model_size": size}).json()["text"] == "Bonjour"
    assert http.post("/v1/clean", json={"text": "Bonjour"}).status_code == 200
    assert worker.requests == [("k2-cleaner", size), ("k2-cleaner", None)]
    assert http.post("/v1/clean", json={"text": "Bonjour", "model_size": "7B"}).status_code == 422
    assert len(worker.requests) == 2


def test_pi_model_ids_select_the_agent_role_and_size(recording_gateway):
    http, worker = recording_gateway
    for model in ("k2-1b", "k2-4b", "k2-agent"):
        response = http.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "Bonjour"}]})
        assert response.status_code == 200 and response.json()["model"] == "k2-agent"
    assert worker.requests == [("k2-agent", "1B"), ("k2-agent", "4B"), ("k2-agent", None)]
    assert http.post("/v1/chat/completions", json={"model": "k2-7b", "messages": [{"role": "user", "content": "x"}]}).status_code == 404


@pytest.mark.parametrize("size", ["1B", "4B"])
def test_cleanup_and_agent_share_identical_weight_and_tokenizer_instances(size):
    worker = Worker()
    try:
        worker.models = {
            "k2-cleaner": (object(), object(), {"max_position_embeddings":131072}, {"display_name":"MBZUAI · 1B", "context_limit":4096}),
            "k2-agent": (object(), object(), {"max_position_embeddings":32768}, {"display_name":"MBZUAI · 4B", "context_limit":16384}),
        }
        requests = [CompletionRequest(model=role, model_size=size, messages=[{"role":"user","content":"test"}]) for role in ("k2-cleaner","k2-agent")]
        cleaner, agent = [worker.resolve_model(request) for request in requests]
        assert cleaner[0] is agent[0]
        assert cleaner[1] is agent[1]
        assert cleaner[2] is agent[2]
        assert cleaner[3]["context_limit"] == 4096
        assert agent[3]["context_limit"] == 16384
    finally:
        worker.executor.shutdown()
