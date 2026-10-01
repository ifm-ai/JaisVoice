"""Observe real worker load transitions using explicit weight/Metal test doubles."""
import sys
import threading
import time
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from k2_runtime import gateway
from model_fixtures import inventory


@pytest.fixture
def loading_fixture(monkeypatch, tmp_path):
    worker = gateway.Worker()
    installed = inventory(tmp_path, ("1B", "4B", "7B"), context_limit=512, reasoning_efforts=["low"])
    installed["7B"]["installed"] = False
    monkeypatch.setattr(gateway, "worker", worker)
    monkeypatch.setattr(gateway, "read_inventory", lambda: {size: dict(entry) for size, entry in installed.items()})
    monkeypatch.setattr(gateway, "prepare_k2_prompt", lambda *args: ([], None))
    monkeypatch.delenv("HORIZON_DEFAULT_MODEL_SIZE", raising=False)

    fake_model = ModuleType("k2_runtime.model")
    fake_mlx = ModuleType("mlx")
    fake_mlx.__path__ = []
    fake_core = ModuleType("mlx.core")
    fake_mlx.core = fake_core
    fake_core.array = lambda value: value
    fake_core.eval = lambda value: None
    fake_core.get_active_memory = lambda: 0
    fake_core.clear_cache = lambda: None
    fake_model.load_model = lambda path: (lambda value: path.name, None, {"max_position_embeddings": 512})
    monkeypatch.setitem(sys.modules, "k2_runtime.model", fake_model)
    monkeypatch.setitem(sys.modules, "mlx", fake_mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", fake_core)
    try:
        yield worker, installed, fake_model, fake_core
    finally:
        worker.executor.shutdown(wait=True, cancel_futures=True)


def states(snapshot):
    return {size: entry["status"] for size, entry in snapshot["models"].items()}


def test_health_responds_during_load_and_ready_follows_warmup(loading_fixture):
    worker, _, fake_model, fake_core = loading_fixture
    load_entered, load_release = threading.Event(), threading.Event()
    warm_entered, warm_release = threading.Event(), threading.Event()
    finished = threading.Event()
    original_load = worker.load

    def load():
        try:
            original_load()
        finally:
            finished.set()
    worker.load = load

    def load_model(path):
        load_entered.set()
        assert load_release.wait(5), "Test failed to release model loading"
        return lambda value: path.name, None, {"max_position_embeddings": 512}

    def evaluate(value):
        warm_entered.set()
        assert warm_release.wait(5), "Test failed to release warmup"

    fake_model.load_model = load_model
    fake_core.eval = evaluate
    with TestClient(gateway.app) as http:
        try:
            assert load_entered.wait(3)
            before = time.monotonic()
            snapshot = http.get("/health").json()
            assert time.monotonic() - before < 1
            assert snapshot["status"] == "starting"
            assert states(snapshot) == {"1B": "unloaded", "4B": "loading", "7B": "not_installed"}
            assert http.get("/v1/models").json()["data"] == []
            load_release.set()
            assert warm_entered.wait(3)
            assert states(http.get("/health").json())["4B"] == "loading"
            warm_release.set()
            assert finished.wait(3)
            snapshot = http.get("/health").json()
            assert snapshot["status"] == "ready" and snapshot["message"] == "Prêt · Test 4B"
            assert states(snapshot) == {"1B": "unloaded", "4B": "ready", "7B": "not_installed"}
            assert [model["id"] for model in http.get("/v1/models").json()["data"]] == ["k2-4b"]
            assert all("error" not in entry for entry in snapshot["models"].values())
            assert snapshot["models"]["7B"]["installed"] is False
        finally:
            load_release.set(); warm_release.set()
            assert finished.wait(3)


@pytest.mark.parametrize("default,installed_sizes,expected", [
    ("4B", ["1B"], "1B"), ("1B", ["1B", "4B"], "1B"), ("4B", [], None),
])
def test_startup_loads_the_default_or_first_installed_size(loading_fixture, monkeypatch, default, installed_sizes, expected):
    worker, installed, _, _ = loading_fixture
    monkeypatch.setenv("HORIZON_DEFAULT_MODEL_SIZE", default)
    for size, entry in installed.items():
        entry["installed"] = size in installed_sizes
    worker.load()
    snapshot = worker.health_snapshot()
    assert snapshot["status"] == "ready"
    assert list(worker.models) == ([expected] if expected else [])
    if expected is None:
        assert snapshot["message"] == "Aucun modèle de langage installé"


@pytest.mark.parametrize("phase", ["load", "warmup"])
def test_load_or_warmup_failure_marks_the_model_and_the_service(loading_fixture, phase):
    worker, _, fake_model, fake_core = loading_fixture

    def load_model(path):
        if phase == "load":
            raise RuntimeError("Explicit load failure")
        return lambda value: path.name, None, {"max_position_embeddings": 512}

    def evaluate(value):
        if phase == "warmup":
            raise RuntimeError("Explicit warmup failure")

    fake_model.load_model = load_model
    fake_core.eval = evaluate
    worker.load()
    snapshot = worker.health_snapshot()
    assert snapshot["status"] == "error"
    assert snapshot["models"]["4B"]["error"] == f"Explicit {phase} failure"
    assert states(snapshot) == {"1B": "unloaded", "4B": "error", "7B": "not_installed"}
    assert "error" not in snapshot["models"]["1B"]
    assert worker.model_catalog()["data"] == []


def test_health_snapshot_does_not_expose_mutable_worker_state(loading_fixture):
    worker, _, _, _ = loading_fixture
    worker.load()
    snapshot = worker.health_snapshot()
    snapshot["models"]["1B"]["status"] = "error"
    assert worker.health_snapshot()["models"]["1B"]["status"] == "unloaded"


def test_catalog_error_remains_a_truthful_global_error(loading_fixture, monkeypatch):
    worker, _, _, _ = loading_fixture
    def invalid():
        raise ValueError("Explicit invalid catalog")
    monkeypatch.setattr(gateway, "read_inventory", invalid)
    worker.load()
    snapshot = worker.health_snapshot()
    assert snapshot["status"] == "error"
    assert "Explicit invalid catalog" in snapshot["message"]
    assert snapshot["models"] == {}
