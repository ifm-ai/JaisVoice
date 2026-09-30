"""Observe real worker load transitions using explicit weight/Metal test doubles."""
import sys
import threading
import time
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from k2_runtime import gateway


@pytest.fixture
def loading_fixture(monkeypatch, tmp_path):
    worker = gateway.Worker()
    manifest = {
        alias: {"path": str(tmp_path / alias), "repo": "fixture/" + alias,
                "display_name": "Test " + ("1B" if alias == "k2-cleaner" else "4B"), "context_limit": 512,
                "reasoning_efforts": ["low"]}
        for alias in gateway.MODEL_ALIASES
    }
    monkeypatch.setattr(gateway, "worker", worker)
    monkeypatch.setattr(gateway, "read_model_manifest", lambda path: manifest)
    monkeypatch.setattr(gateway, "prepare_k2_prompt", lambda *args: ([], None))

    fake_model = ModuleType("k2_runtime.model")
    fake_mlx = ModuleType("mlx")
    fake_mlx.__path__ = []
    fake_core = ModuleType("mlx.core")
    fake_mlx.core = fake_core
    fake_core.array = lambda value: value
    fake_core.eval = lambda value: None
    fake_core.get_active_memory = lambda: 0
    fake_model.load_model = lambda path: (lambda value: path.name, None, {"max_position_embeddings": 512})
    monkeypatch.setitem(sys.modules, "k2_runtime.model", fake_model)
    monkeypatch.setitem(sys.modules, "mlx", fake_mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", fake_core)
    try:
        yield worker, manifest, fake_model, fake_core
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)


def states(snapshot):
    return {alias: entry["status"] for alias, entry in snapshot["models"].items()}


def test_health_responds_during_each_load_and_ready_follows_warmup(loading_fixture):
    worker, manifest, fake_model, fake_core = loading_fixture
    aliases = gateway.MODEL_ALIASES
    load_entered = {alias: threading.Event() for alias in aliases}
    load_release = {alias: threading.Event() for alias in aliases}
    warm_entered = {alias: threading.Event() for alias in aliases}
    warm_release = {alias: threading.Event() for alias in aliases}
    finished = threading.Event()
    original_load = worker.load

    def load():
        try:
            original_load()
        finally:
            finished.set()
    worker.load = load

    def load_model(path):
        load_entered[path.name].set()
        assert load_release[path.name].wait(5), "Test failed to release model loading"
        return lambda value: path.name, None, {"max_position_embeddings": 512}

    def evaluate(alias):
        warm_entered[alias].set()
        assert warm_release[alias].wait(5), "Test failed to release warmup"

    fake_model.load_model = load_model
    fake_core.eval = evaluate
    with TestClient(gateway.app) as http:
        try:
            assert load_entered["k2-agent"].wait(3)
            before = time.monotonic()
            snapshot = http.get("/health").json()
            assert time.monotonic() - before < 1
            assert snapshot["status"] == "starting"
            assert states(snapshot) == {"k2-cleaner": "unloaded", "k2-agent": "loading"}
            assert http.get("/v1/models").json()["data"] == []
            assert not load_entered["k2-cleaner"].is_set()
            load_release["k2-agent"].set()
            assert warm_entered["k2-agent"].wait(3)
            assert states(http.get("/health").json())["k2-agent"] == "loading"
            warm_release["k2-agent"].set()

            assert finished.wait(3)
            snapshot = http.get("/health").json()
            assert snapshot["status"] == "ready"
            assert states(snapshot) == {"k2-cleaner": "unloaded", "k2-agent": "ready"}
            assert [model["id"] for model in http.get("/v1/models").json()["data"]] == ["k2-agent"]
            assert all("error" not in entry for entry in snapshot["models"].values())
        finally:
            for event in [*load_release.values(), *warm_release.values()]:
                event.set()
            assert finished.wait(3)


@pytest.mark.parametrize("failed_alias", ["k2-agent"])
@pytest.mark.parametrize("phase", ["load", "warmup"])
def test_load_or_warmup_failure_marks_failed_and_unstarted_models(loading_fixture, failed_alias, phase):
    worker, manifest, fake_model, fake_core = loading_fixture
    loaded = []

    def load_model(path):
        loaded.append(path.name)
        if path.name == failed_alias and phase == "load":
            raise RuntimeError("Explicit load failure")
        return lambda value: path.name, None, {"max_position_embeddings": 512}

    def evaluate(alias):
        if alias == failed_alias and phase == "warmup":
            raise RuntimeError("Explicit warmup failure")

    fake_model.load_model = load_model
    fake_core.eval = evaluate
    worker.load()
    snapshot = worker.health_snapshot()
    assert snapshot["status"] == "error"
    assert snapshot["models"][failed_alias]["error"] == f"Explicit {phase} failure"
    assert loaded == ["k2-agent"]
    assert states(snapshot) == {"k2-cleaner": "unloaded", "k2-agent": "error"}
    assert "error" not in snapshot["models"]["k2-cleaner"]
    assert worker.model_catalog()["data"] == []
    for alias in gateway.MODEL_ALIASES:
        assert snapshot["models"][alias]["repo"] == manifest[alias]["repo"]


def test_health_snapshot_does_not_expose_mutable_worker_state(loading_fixture):
    worker, _, _, _ = loading_fixture
    worker.load()
    snapshot = worker.health_snapshot()
    snapshot["models"]["k2-cleaner"]["status"] = "error"
    snapshot["models"]["k2-agent"]["reasoning_efforts"].append("invalid")
    second = worker.health_snapshot()
    assert second["models"]["k2-cleaner"]["status"] == "unloaded"
    assert second["models"]["k2-agent"]["reasoning_efforts"] == ["low"]


def test_manifest_error_remains_a_truthful_global_error(loading_fixture, monkeypatch):
    worker, _, _, _ = loading_fixture
    def invalid(path):
        raise ValueError("Explicit invalid manifest")
    monkeypatch.setattr(gateway, "read_model_manifest", invalid)
    worker.load()
    snapshot = worker.health_snapshot()
    assert snapshot["status"] == "error"
    assert snapshot["message"] == "Explicit invalid manifest"
    assert snapshot["models"] == {}
