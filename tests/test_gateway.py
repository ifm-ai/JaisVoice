"""The gateway's model memory and HTTP framing, with explicit MLX and model test doubles."""
import json
import sys
import threading
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from conftest import write_model
from jais_runtime import gateway


@pytest.fixture
def client(store, monkeypatch):
    """The real app on a fresh worker; 0.9B and 3.7B installed, 7B not; loading never touches Metal."""
    _, base, _ = store
    for size in ("0.9B", "3.7B"):
        write_model(base / f"model-{size}")
    fake_core = ModuleType("mlx.core")
    fake_core.array, fake_core.eval, fake_core.clear_cache, fake_core.get_active_memory = (lambda x: x), (lambda x: None), (lambda: None), (lambda: 0)
    fake_mlx = ModuleType("mlx"); fake_mlx.__path__ = []; fake_mlx.core = fake_core
    fake_model = ModuleType("jais_runtime.model")
    fake_model.load_model = lambda path: ((lambda tokens: tokens), path.name, {})
    for name, module in [("mlx", fake_mlx), ("mlx.core", fake_core), ("jais_runtime.model", fake_model)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(gateway, "prepare_prompt", lambda *args: ([], None))
    monkeypatch.delenv("JAIS_MODEL_SIZE", raising=False)
    worker = gateway.Worker()
    monkeypatch.setattr(gateway, "worker", worker)
    with TestClient(gateway.app) as http:
        worker.executor.submit(lambda: None).result()  # startup load finished
        yield http, worker, fake_model


def statuses(response):
    return {size: model["status"] for size, model in response.json()["models"].items()}


def test_startup_loads_the_default_size(client):
    http, worker, _ = client
    assert http.get("/health").json()["status"] == "ready"
    assert statuses(http.get("/health")) == {"0.9B": "unloaded", "3.7B": "ready", "7B": "not_installed"}


def test_startup_loads_the_size_the_app_asked_for(store, monkeypatch):
    _, base, _ = store
    write_model(base / "model-0.9B")
    write_model(base / "model-3.7B")
    monkeypatch.setenv("JAIS_MODEL_SIZE", "0.9B")
    worker = gateway.Worker()
    monkeypatch.setattr(worker, "change", lambda size, action: setattr(worker, "model", (size, None, None)))
    worker.start()
    assert worker.loaded_size == "0.9B" and worker.status == "ready"


def test_one_model_stays_resident(client):
    http, worker, _ = client
    assert statuses(http.post("/models/0.9B/memory", json={"action": "load"})) == {"0.9B": "ready", "3.7B": "unloaded", "7B": "not_installed"}
    assert statuses(http.post("/models/3.7B/memory", json={"action": "unload"}))["0.9B"] == "ready"  # not resident: no change
    assert statuses(http.post("/models/0.9B/memory", json={"action": "unload"}))["0.9B"] == "unloaded"
    assert worker.model is None and worker.status == "ready"


def test_missing_or_unknown_models_cannot_be_loaded(client):
    http, worker, _ = client
    response = http.post("/models/7B/memory", json={"action": "load"})
    assert response.status_code == 500 and "not installed" in response.json()["detail"]
    assert http.post("/models/13B/memory", json={"action": "load"}).status_code == 422
    assert worker.loaded_size == "3.7B"


def test_load_failure_is_reported_for_that_size(client):
    http, worker, loader = client
    def fail(path): raise ValueError("test missing weights")
    loader.load_model = fail
    assert http.post("/models/0.9B/memory", json={"action": "load"}).status_code == 500
    model = http.get("/health").json()["models"]["0.9B"]
    assert model["status"] == "error" and model["error"] == "test missing weights"
    assert worker.model is None and not worker.changing


def test_memory_changes_wait_for_generations(client):
    http, worker, _ = client
    worker.pending = 1
    assert http.post("/models/0.9B/memory", json={"action": "load"}).status_code == 409
    assert worker.loaded_size == "3.7B"


def chat(http, model="jais-3-3.7b"):
    return http.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True})


def test_the_model_id_selects_a_loaded_size(client):
    http, _, _ = client
    assert chat(http, "unknown").status_code == 404
    assert chat(http, "jais-3-0.9b").status_code == 409


def test_streamed_tool_calls_end_with_done(client, monkeypatch):
    http, worker, _ = client
    jobs = []
    def fake_generate(job, canceled, emit):
        jobs.append(job)
        emit(("ready", None))
        emit(("delta", {"content": "Opening"}))
        emit(("delta", {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "browser_navigate", "arguments": "{}"}}]}))
        emit(("done", {"finish_reason": "tool_calls", "content": "Opening", "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}))
        emit(("end", None))
    monkeypatch.setattr(worker, "generate", fake_generate)
    events = [json.loads(line[6:]) for line in chat(http).text.splitlines() if line.startswith("data: {")]
    assert events[2]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls" and events[-1]["usage"]["total_tokens"] == 5
    assert jobs[0].size == "3.7B" and jobs[0].context == gateway.AGENT_CONTEXT
    assert worker.pending == 0 and not worker.cancel_flags


def test_streamed_error_is_explicit_not_success(client, monkeypatch):
    http, worker, _ = client
    def fake_generate(job, canceled, emit):
        emit(("ready", None))
        emit(("error", "The generation reached its token limit before completing"))
        emit(("end", None))
    monkeypatch.setattr(worker, "generate", fake_generate)
    response = chat(http)
    assert response.status_code == 200 and "incomplete_generation" in response.text and "[DONE]" not in response.text
    assert worker.pending == 0


def test_queued_cancel_does_not_enter_model_generation():
    worker = gateway.Worker()
    flag = threading.Event()
    flag.set()
    emitted = []
    worker.generate(None, flag, emitted.append)
    assert emitted == [("end", None)]
    worker.executor.shutdown()


def test_agent_sampling_follows_the_model_card_and_cleanup_stays_greedy(client, monkeypatch):
    http, worker, _ = client
    jobs = []
    def fake_generate(job, canceled, emit):
        jobs.append(job)
        emit(("ready", None)); emit(("done", {"finish_reason": "stop", "content": "ok", "usage": {}})); emit(("end", None))
    monkeypatch.setattr(worker, "generate", fake_generate)
    chat(http)
    agent = jobs[-1]
    assert (agent.temperature, agent.top_p, agent.top_k) == (0.7, 0.95, 20)
    assert agent.context == 32768 and agent.max_tokens == 8192


def test_output_budget_shrinks_to_the_context_left():
    assert gateway.output_budget(1000, 16384, 32768) == 16384
    assert gateway.output_budget(30000, 16384, 32768) == 2768
    with pytest.raises(ValueError, match="Context budget exceeded"):
        gateway.output_budget(32700, 16384, 32768)
    assert gateway.output_budget(100, 50, 200) == 50


def test_remote_mode_loads_no_local_model(store, monkeypatch):
    _, base, _ = store
    write_model(base / "model-3.7B")
    monkeypatch.setenv("JAIS_MODEL_SIZE", gateway.NO_LOCAL_MODEL)
    worker = gateway.Worker()
    monkeypatch.setattr(worker, "change", lambda size, action: pytest.fail("no model may load"))
    worker.start()
    assert worker.loaded_size is None and worker.status == "ready" and worker.message == "Ready"
