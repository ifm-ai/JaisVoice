"""Model catalog storage, downloads and launchers with tiny real files; no network or Metal."""
import json
import os
from pathlib import Path
import shutil
import sys
import threading

import numpy as np
import pytest
from safetensors.numpy import save_file

from k2_runtime import model_store
from k2_runtime.checkpoint import expected_weight_shapes
from scripts import models as models_cli
from scripts import run_agent, run_services

REVISION = "b" * 40


def write_model(directory, quantized=False):
    directory.mkdir(parents=True)
    config = dict(model_type="k2_horizon", hidden_size=128, intermediate_size=256,
        num_attention_heads=2, num_key_value_heads=1, head_dim=64, vocab_size=128,
        num_hidden_layers=1, layernorm_num_groups=2, max_position_embeddings=16384)
    if quantized:
        config["quantization"] = {"bits": 8, "group_size": 64, "mode": "affine"}
    weights = {}
    for name, shape in expected_weight_shapes(config).items():
        if quantized and len(shape) == 2:
            weights[name] = np.zeros((shape[0], shape[1] // 4), dtype=np.uint32)
            weights[name.removesuffix(".weight") + ".scales"] = np.ones((shape[0], shape[1] // 64), dtype=np.float32)
            weights[name.removesuffix(".weight") + ".biases"] = np.zeros((shape[0], shape[1] // 64), dtype=np.float32)
        else:
            weights[name] = np.ones(shape, dtype=np.float32)
    shard = "model-00001-of-00001.safetensors"
    save_file(weights, directory / shard)
    (directory / "config.json").write_text(json.dumps(config))
    (directory / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {name: shard for name in weights}}))
    (directory / "tokenizer.json").write_text('{"model": {"type": "WordLevel", "vocab": {"test": 0}}}')
    (directory / "tokenizer_config.json").write_text('{"tokenizer_class": "PreTrainedTokenizerFast"}')
    (directory / "chat_template.jinja").write_text('{{ messages }}')


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temporary runtime and catalog: an ASR, a published 4B and an unpublished 1B."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"format": "jais-voice-models-v1", "models": [
        {"id": "asr-f5", "kind": "asr", "display_name": "ASR", "directory": "ASR",
         "download": {"repo": "org/asr", "revision": REVISION, "profile": "F5"}},
        {"id": "k2-1b", "kind": "llm", "size": "1B", "display_name": "Test 1B", "directory": "model-1B", "download": None},
        {"id": "k2-4b", "kind": "llm", "size": "4B", "display_name": "Test 4B", "directory": "model-4B",
         "download": {"repo": "org/model-4b", "revision": REVISION}},
    ]}))
    monkeypatch.setenv("HORIZON_MODEL_CATALOG", str(catalog))
    base = tmp_path / "runtime/models"
    return model_store.load_catalog(), base, tmp_path


class FakeHub:
    """Stands in for huggingface_hub; copies prepared files like a snapshot."""
    def __init__(self, source, interrupt_after=None):
        self.source, self.interrupt_after, self.calls = Path(source), interrupt_after, []
        hub = self
        class HfApi:
            def model_info(self, repo, revision=None, files_metadata=False):
                files = [path for path in hub.source.rglob("*") if path.is_file()]
                return type("Info", (), {"siblings": [type("Sibling", (), {"size": path.stat().st_size})() for path in files]})()
        self.HfApi = HfApi

    def hf_hub_download(self, repo, filename, revision=None, local_dir=None):
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source / filename, target)
        return str(target)

    def snapshot_download(self, repo, revision=None, local_dir=None, allow_patterns=None, max_workers=None):
        self.calls.append((repo, revision, allow_patterns))
        (Path(local_dir) / ".cache/huggingface").mkdir(parents=True, exist_ok=True)
        for index, path in enumerate(sorted(path for path in self.source.rglob("*") if path.is_file())):
            relative = path.relative_to(self.source)
            if allow_patterns is not None and str(relative) not in allow_patterns:
                continue
            if self.interrupt_after is not None and index >= self.interrupt_after:
                self.interrupt_after = None
                raise KeyboardInterrupt
            target = Path(local_dir) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def asr_repository(directory):
    weights = directory / "models/tensors/f5.safetensors"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"asr weights")
    (directory / "requirements-mac.txt").write_text("mlx\n")
    (directory / "install.sh").write_text('mkdir -p .venv/bin && ln -s "$PYTHON_BIN" .venv/bin/python\n')
    files = [{"path": str(path.relative_to(directory)), "bytes": path.stat().st_size, "sha256": model_store.sha256(path)}
             for path in (weights,)]
    (directory / "manifests").mkdir()
    (directory / "manifests/F5.json").write_text(json.dumps({"files": files, "download_files": [item["path"] for item in files]}))
    (directory / "unrelated-profile.bin").write_bytes(b"not part of F5")
    return directory


def test_states_follow_disk_and_publication(store):
    catalog, base, tmp = store
    states = {state["id"]: state for state in (model_store.state(entry, base) for entry in catalog)}
    assert [states[i]["installed"] for i in ("asr-f5", "k2-1b", "k2-4b")] == [False, False, False]
    assert states["k2-1b"]["downloadable"] is False and states["k2-4b"]["downloadable"] is True
    write_model(base / "model-1B")
    state = model_store.state(model_store.find(catalog, "k2-1b"), base)
    assert state["installed"] and state["disk_bytes"] > 0 and state["size"] == "1B"


def test_llm_download_is_validated_then_atomically_installed(store):
    catalog, base, tmp = store
    write_model(tmp / "repo", quantized=True)
    events = []
    hub = FakeHub(tmp / "repo")
    target = model_store.download(model_store.find(catalog, "k2-4b"), base, events.append, hub=hub)
    assert target == base / "model-4B" and (target / "config.json").is_file()
    assert hub.calls == [("org/model-4b", REVISION, None)]
    assert not (base / ".model-4B.download").exists() and not (target / ".cache").exists()
    assert json.loads((target / model_store.PROVENANCE).read_text())["revision"] == REVISION
    assert events[-1] == {"event": "done", "id": "k2-4b"}
    progress = [event for event in events if event.get("phase") == "download"]
    assert progress[-1]["bytes"] == progress[-1]["total"] > 0
    with pytest.raises(RuntimeError, match="already installed"):
        model_store.download(model_store.find(catalog, "k2-4b"), base, events.append, hub=hub)


def test_invalid_download_is_never_installed(store):
    catalog, base, tmp = store
    write_model(tmp / "repo")
    config = json.loads((tmp / "repo/config.json").read_text())
    config["vocab_size"] += 1
    (tmp / "repo/config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="shape or type"):
        model_store.download(model_store.find(catalog, "k2-4b"), base, lambda event: None, hub=FakeHub(tmp / "repo"))
    assert not (base / "model-4B").exists() and (base / ".model-4B.download").is_dir()


def test_interrupted_download_resumes(store):
    catalog, base, tmp = store
    write_model(tmp / "repo")
    entry = model_store.find(catalog, "k2-4b")
    with pytest.raises(KeyboardInterrupt):
        model_store.download(entry, base, lambda event: None, hub=FakeHub(tmp / "repo", interrupt_after=2))
    assert model_store.state(entry, base)["partial_bytes"] > 0 and not model_store.is_installed(entry, base)
    model_store.download(entry, base, lambda event: None, hub=FakeHub(tmp / "repo"))
    assert model_store.is_installed(entry, base)


def test_unpublished_model_cannot_be_downloaded(store):
    catalog, base, _ = store
    with pytest.raises(RuntimeError, match="not published"):
        model_store.download(model_store.find(catalog, "k2-1b"), base, lambda event: None, hub=object())


def test_asr_download_fetches_one_profile_verifies_and_installs_its_environment(store):
    catalog, base, tmp = store
    hub = FakeHub(asr_repository(tmp / "asr-repo"))
    entry = model_store.find(catalog, "asr-f5")
    model_store.download(entry, base, lambda event: None, hub=hub, python=sys.executable)
    assert model_store.is_installed(entry, base)
    assert (base / "ASR/.venv/bin/python").resolve() == Path(sys.executable).resolve()
    assert not (base / "ASR/unrelated-profile.bin").exists()
    assert "manifests/F5.json" in hub.calls[0][2]


def test_altered_asr_file_is_refused(store):
    catalog, base, tmp = store
    repository = asr_repository(tmp / "asr-repo")
    (repository / "models/tensors/f5.safetensors").write_bytes(b"ASR WEIGHTS")
    with pytest.raises(RuntimeError, match="altered"):
        model_store.download(model_store.find(catalog, "asr-f5"), base, lambda event: None, hub=FakeHub(repository), python=sys.executable)
    assert not (base / "ASR").exists()


def test_delete_removes_model_and_partial_download_only(store):
    catalog, base, tmp = store
    write_model(base / "model-4B")
    (base / ".model-4B.download").mkdir()
    write_model(base / "model-1B")
    model_store.delete(model_store.find(catalog, "k2-4b"), base)
    assert not (base / "model-4B").exists() and not (base / ".model-4B.download").exists()
    assert not (base / ".model-4B.deleting").exists()
    assert model_store.is_installed(model_store.find(catalog, "k2-1b"), base)
    model_store.delete(model_store.find(catalog, "k2-4b"), base)  # nothing left: no error


def test_symlinked_model_is_not_followed(store):
    catalog, base, tmp = store
    write_model(tmp / "elsewhere")
    base.mkdir(parents=True)
    (base / "model-4B").symlink_to(tmp / "elsewhere")
    with pytest.raises(RuntimeError, match="symbolic link"):
        model_store.delete(model_store.find(catalog, "k2-4b"), base)
    assert (tmp / "elsewhere/config.json").is_file()


def test_one_operation_per_model(store):
    catalog, base, _ = store
    entry = model_store.find(catalog, "k2-4b")
    with model_store.operation_lock(entry, base):
        with pytest.raises(RuntimeError, match="already in progress"):
            model_store.delete(entry, base)


def test_cli_lists_and_reports_errors_as_json(store, capsys):
    _, base, tmp = store
    runtime = ["--runtime-dir", str(tmp / "runtime")]
    assert models_cli.main([*runtime, "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [model["id"] for model in listing["models"]] == ["asr-f5", "k2-1b", "k2-4b"]
    assert models_cli.main([*runtime, "download", "k2-1b"]) == 1
    assert "not published" in json.loads(capsys.readouterr().out)["message"]
    assert models_cli.main([*runtime, "download", "missing"]) == 1
    assert json.loads(capsys.readouterr().out)["event"] == "error"
    write_model(base / "model-1B")
    assert models_cli.main([*runtime, "delete", "k2-1b"]) == 0
    assert json.loads(capsys.readouterr().out) == {"event": "deleted", "id": "k2-1b"}
    assert not (base / "model-1B").exists()


# The supervisor: the gateway always runs, the ASR only while installed.

class Child:
    launched = []
    def __init__(self, command, **kwargs):
        self.pid = 100000 + len(Child.launched)
        self.command = command
        Child.launched.append((command, kwargs))
    def poll(self):
        return None
    def wait(self, **kwargs):
        return 0


def run_supervisor(monkeypatch, tmp, iterations, during=lambda index: None):
    Child.launched = []
    stopped = []
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connect_ex(self, address): return 1
    class Stop:
        count = 0
        def is_set(self): return False
        def wait(self, timeout):
            during(Stop.count)
            Stop.count += 1
            return Stop.count > iterations
    monkeypatch.setattr(run_services, "RUNTIME", tmp / "runtime")
    monkeypatch.setattr(run_services.subprocess, "Popen", Child)
    monkeypatch.setattr(run_services.socket, "socket", Probe)
    monkeypatch.setattr(run_services.signal, "signal", lambda *args: None)
    monkeypatch.setattr(run_services.os, "killpg", lambda pid, sig: stopped.append(pid))
    monkeypatch.setattr(run_services, "STOP", Stop())
    assert run_services.main() == 0
    return [("mlx" if "k2_runtime.gateway" in command else "asr") for command, _ in Child.launched], stopped


def install_asr(base):
    (base / "ASR/.venv/bin").mkdir(parents=True, exist_ok=True)
    (base / "ASR/.venv/bin/python").write_text("")
    (base / "ASR/manifests").mkdir(exist_ok=True)
    (base / "ASR/manifests/F5.json").write_text("{}")


def test_supervisor_without_asr_starts_only_the_gateway(store, monkeypatch):
    _, _, tmp = store
    names, _ = run_supervisor(monkeypatch, tmp, iterations=2)
    assert names == ["mlx"]
    env = Child.launched[0][1]["env"]
    assert env["HORIZON_MODEL_CATALOG"] == os.environ["HORIZON_MODEL_CATALOG"]
    assert env["HORIZON_ASR_MODEL_DIR"] == str(tmp / "runtime/models/ASR")
    assert not (tmp / "runtime/services.json").exists()


def test_supervisor_starts_asr_after_download_and_stops_it_after_deletion(store, monkeypatch):
    _, base, tmp = store
    def during(index):
        if index == 1:
            install_asr(base)
        if index == 3:
            shutil.rmtree(base / "ASR")
    names, stopped = run_supervisor(monkeypatch, tmp, iterations=5, during=during)
    assert names == ["mlx", "asr"]
    asr_pid = 100001
    assert stopped.count(asr_pid) >= 2  # SIGTERM then SIGKILL of its group on deletion


def test_supervisor_starts_installed_asr_first(store, monkeypatch):
    _, base, tmp = store
    install_asr(base)
    names, _ = run_supervisor(monkeypatch, tmp, iterations=1)
    assert names == ["asr", "mlx"]


def test_invalid_catalog_stops_before_any_service(store, monkeypatch):
    _, _, tmp = store
    Path(os.environ["HORIZON_MODEL_CATALOG"]).write_text("{}")
    monkeypatch.setattr(run_services, "RUNTIME", tmp / "runtime")
    monkeypatch.setattr(run_services.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("no service may start"))
    with pytest.raises(ValueError, match="catalog format"):
        run_services.main()


@pytest.mark.parametrize("override,expected", [(None, "low"), ("medium", "medium")])
def test_agent_launcher_uses_isolated_configuration(store, monkeypatch, override, expected):
    _, _, tmp = store
    monkeypatch.setattr(run_agent, "RUNTIME", tmp / "runtime")
    env = {"HORIZON_NODE": run_agent.find_node({})}
    if override:
        env["HORIZON_AGENT_REASONING_EFFORT"] = override
    argv, child_env, workspace = run_agent.command(env)
    assert argv[argv.index("--thinking") + 1] == expected
    assert argv[argv.index("--mode") + 1] == "rpc" and "--no-session" in argv and "--no-builtin-tools" in argv
    assert argv[argv.index("--no-extensions") + 1:argv.index("--no-extensions") + 5] == ["-e", "builtin:mcp", "-e", str(run_agent.ROOT / "agent/extensions/jais")]
    agent_dir = tmp / "runtime/pi-agent"
    assert child_env["PI_CODING_AGENT_DIR"] == str(agent_dir) and child_env["PI_OFFLINE"] == "1"
    assert all((agent_dir / name).read_text() == (run_agent.ROOT / "agent" / name).read_text() for name in run_agent.CONFIG_FILES)
    assert workspace == tmp / "runtime/agent-workspace" and workspace.is_dir()
    with pytest.raises(ValueError):
        run_agent.command({**env, "HORIZON_AGENT_REASONING_EFFORT": "max"})


def test_pi_knows_every_catalog_size():
    ids = {model["id"] for model in json.loads((run_agent.ROOT / "agent/models.json").read_text())["providers"]["jais"]["models"]}
    assert ids == {f"k2-{size.lower()}" for size in model_store.SIZES}


def test_bundled_asr_python_is_copied_out_of_the_app(tmp_path, monkeypatch):
    bundled = tmp_path / "App.app/runtime/python3.11"
    (bundled / "bin").mkdir(parents=True)
    (bundled / "bin/python3.11").write_text("#!/bin/sh\n")
    (bundled / "bin/python3").symlink_to("python3.11")
    monkeypatch.setenv("HORIZON_ASR_PYTHON_HOME", str(bundled))
    monkeypatch.setenv("HORIZON_RUNTIME_DIR", str(tmp_path / "runtime"))
    python = Path(model_store.find_python311())
    assert python == tmp_path / "runtime/python3.11/bin/python3.11" and python.is_file()
    assert (python.parent / "python3").is_symlink()
    # Later calls reuse the copy, even after the app has moved.
    shutil.rmtree(tmp_path / "App.app")
    assert Path(model_store.find_python311()) == python
