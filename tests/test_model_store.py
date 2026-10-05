"""Model catalog, downloads, the supervisor and the agent launcher, with tiny real files; no network or Metal."""
import fnmatch
import json
from pathlib import Path
import shutil
import sys

import pytest

from conftest import REVISION, write_model
from jais_runtime import model_store
from scripts import models as models_cli
from scripts import run_agent, run_services


class RepositoryNotFoundError(Exception):
    """Named like huggingface_hub's error for a private repository."""


class FakeHub:
    """Stands in for huggingface_hub; copies prepared files like a snapshot, with LFS hashes."""
    def __init__(self, source, interrupt_after=None, error=None):
        self.source, self.interrupt_after, self.error, self.calls = Path(source), interrupt_after, error, []
        hub = self
        class HfApi:
            def model_info(self, repo, revision=None, files_metadata=False):
                if hub.error:
                    raise hub.error
                siblings = []
                for path in sorted(p for p in hub.source.rglob("*") if p.is_file()):
                    lfs = type("Lfs", (), {"sha256": model_store.sha256(path)})() if path.suffix == ".safetensors" else None
                    siblings.append(type("Sibling", (), {"rfilename": str(path.relative_to(hub.source)), "size": path.stat().st_size, "lfs": lfs})())
                return type("Info", (), {"siblings": siblings})()
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
            if allow_patterns is not None and not any(fnmatch.fnmatch(str(relative), pattern) for pattern in allow_patterns):
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
    files = [{"path": str(weights.relative_to(directory)), "bytes": weights.stat().st_size, "sha256": model_store.sha256(weights)}]
    (directory / "manifests").mkdir()
    (directory / "manifests/F5.json").write_text(json.dumps({"files": files, "download_files": [item["path"] for item in files]}))
    (directory / "unrelated-profile.bin").write_bytes(b"not part of F5")
    return directory


def test_catalog_lists_the_three_jais_sizes():
    catalog = model_store.load_catalog()
    assert [entry["size"] for entry in catalog if entry["kind"] == "llm"] == list(model_store.SIZES)
    assert all(entry["download"]["repo"].startswith("IFM/Jais-3-") for entry in catalog if entry["kind"] == "llm")


@pytest.mark.parametrize("change", [{"revision": "main"}, {"repo": "not a repo"}])
def test_invalid_catalog_entry_rejects_the_catalog(tmp_path, change):
    entry = {"id": "jais-3-3.7b", "kind": "llm", "size": "3.7B", "display_name": "3.7B", "directory": "model",
             "download": {"repo": "org/model", "revision": REVISION, **change}}
    (tmp_path / "catalog.json").write_text(json.dumps({"models": [entry]}))
    with pytest.raises(ValueError):
        model_store.load_catalog(tmp_path / "catalog.json")


def test_states_follow_disk(store):
    catalog, base, _ = store
    entry = model_store.find(catalog, "jais-3-0.9b")
    assert not model_store.state(entry, base)["installed"]
    write_model(base / "model-0.9B")
    state = model_store.state(entry, base)
    assert state["installed"] and state["disk_bytes"] > 0 and state["size"] == "0.9B"


def test_download_is_validated_then_atomically_installed(store):
    catalog, base, tmp = store
    write_model(tmp / "repo", quantized=True)
    (tmp / "repo/modeling.py").write_text("raise SystemExit('never fetched')")
    events = []
    hub = FakeHub(tmp / "repo")
    target = model_store.download(model_store.find(catalog, "jais-3-3.7b"), base, events.append, hub=hub)
    assert target == base / "model-3.7B" and (target / "config.json").is_file()
    assert hub.calls == [("org/model-3.7B", REVISION, model_store.MODEL_PATTERNS)]
    assert not (target / "modeling.py").exists() and not (target / ".cache").exists()
    assert not (base / ".model-3.7B.download").exists()
    assert json.loads((target / model_store.PROVENANCE).read_text())["revision"] == REVISION
    assert events[-1] == {"event": "done", "id": "jais-3-3.7b"}
    progress = [event for event in events if event.get("phase") == "download"]
    assert progress[-1]["bytes"] == progress[-1]["total"] > 0
    with pytest.raises(RuntimeError, match="already installed"):
        model_store.download(model_store.find(catalog, "jais-3-3.7b"), base, events.append, hub=hub)


def test_invalid_download_is_never_installed(store):
    catalog, base, tmp = store
    write_model(tmp / "repo")
    config = json.loads((tmp / "repo/config.json").read_text())
    config["vocab_size"] += 1
    (tmp / "repo/config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="shape or type"):
        model_store.download(model_store.find(catalog, "jais-3-3.7b"), base, lambda event: None, hub=FakeHub(tmp / "repo"))
    assert not (base / "model-3.7B").exists() and (base / ".model-3.7B.download").is_dir()


def test_interrupted_download_resumes(store):
    catalog, base, tmp = store
    write_model(tmp / "repo")
    entry = model_store.find(catalog, "jais-3-3.7b")
    with pytest.raises(KeyboardInterrupt):
        model_store.download(entry, base, lambda event: None, hub=FakeHub(tmp / "repo", interrupt_after=2))
    assert model_store.state(entry, base)["partial_bytes"] > 0 and not model_store.is_installed(entry, base)
    model_store.download(entry, base, lambda event: None, hub=FakeHub(tmp / "repo"))
    assert model_store.is_installed(entry, base)


def test_without_access_the_files_downloaded_by_hand_are_installed(store):
    catalog, base, tmp = store
    entry = model_store.find(catalog, "jais-3-3.7b")
    hub = FakeHub(tmp / "unused", error=RepositoryNotFoundError())
    with pytest.raises(model_store.NoAccess) as error:
        model_store.download(entry, base, lambda event: None, hub=hub)
    assert error.value.folder == base / "sources/model-3.7B"
    write_model(base / "sources/model-3.7B")
    model_store.download(entry, base, lambda event: None, hub=hub)
    assert model_store.is_installed(entry, base) and not (base / "sources/model-3.7B").exists()


def test_asr_download_fetches_one_profile_verifies_and_installs_its_environment(store):
    catalog, base, tmp = store
    hub = FakeHub(asr_repository(tmp / "asr-repo"))
    entry = model_store.find(catalog, "asr-f5")
    model_store.download(entry, base, lambda event: None, hub=hub, python=sys.executable)
    assert model_store.is_installed(entry, base)
    assert (base / "ASR/.venv/bin/python").resolve() == Path(sys.executable).resolve()
    assert not (base / "ASR/unrelated-profile.bin").exists()


def test_altered_asr_file_is_refused(store):
    catalog, base, tmp = store
    repository = asr_repository(tmp / "asr-repo")
    (repository / "models/tensors/f5.safetensors").write_bytes(b"ASR WEIGHTS")
    with pytest.raises(RuntimeError, match="altered"):
        model_store.download(model_store.find(catalog, "asr-f5"), base, lambda event: None, hub=FakeHub(repository), python=sys.executable)
    assert not (base / "ASR").exists()


def test_delete_removes_model_and_partial_download_only(store):
    catalog, base, tmp = store
    write_model(base / "model-3.7B")
    (base / ".model-3.7B.download").mkdir()
    write_model(base / "model-0.9B")
    model_store.delete(model_store.find(catalog, "jais-3-3.7b"), base)
    assert not (base / "model-3.7B").exists() and not (base / ".model-3.7B.download").exists()
    assert model_store.is_installed(model_store.find(catalog, "jais-3-0.9b"), base)
    model_store.delete(model_store.find(catalog, "jais-3-3.7b"), base)  # nothing left: no error


def test_symlinked_model_is_not_followed(store):
    catalog, base, tmp = store
    write_model(tmp / "elsewhere")
    base.mkdir(parents=True)
    (base / "model-3.7B").symlink_to(tmp / "elsewhere")
    with pytest.raises(RuntimeError, match="symbolic link"):
        model_store.delete(model_store.find(catalog, "jais-3-3.7b"), base)
    assert (tmp / "elsewhere/config.json").is_file()


def test_one_operation_per_model(store):
    catalog, base, _ = store
    entry = model_store.find(catalog, "jais-3-3.7b")
    with model_store.operation_lock(entry, base):
        with pytest.raises(RuntimeError, match="already in progress"):
            model_store.delete(entry, base)


def test_cli_lists_and_reports_errors_as_json(store, capsys):
    _, base, tmp = store
    runtime = ["--runtime-dir", str(tmp / "runtime")]
    assert models_cli.main([*runtime, "list"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [model["id"] for model in listing["models"]] == ["asr-f5", "jais-3-0.9b", "jais-3-3.7b", "jais-3-7b"]
    assert models_cli.main([*runtime, "download", "missing"]) == 1
    assert json.loads(capsys.readouterr().out)["event"] == "error"
    write_model(base / "model-0.9B")
    assert models_cli.main([*runtime, "delete", "jais-3-0.9b"]) == 0
    assert json.loads(capsys.readouterr().out) == {"event": "deleted", "id": "jais-3-0.9b"}
    assert not (base / "model-0.9B").exists()


# The supervisor: the gateway always runs, the ASR only while installed.

class Child:
    launched = []
    def __init__(self, command, **kwargs):
        self.pid = 100000 + len(Child.launched)
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
    return [("mlx" if "jais_runtime.gateway" in command else "asr") for command, _ in Child.launched], stopped


def install_asr(base):
    (base / "ASR/.venv/bin").mkdir(parents=True, exist_ok=True)
    (base / "ASR/.venv/bin/python").write_text("")
    (base / "ASR/manifests").mkdir(exist_ok=True)
    (base / "ASR/manifests/F5.json").write_text("{}")


def test_supervisor_without_asr_starts_only_the_gateway(store, monkeypatch):
    _, _, tmp = store
    names, _ = run_supervisor(monkeypatch, tmp, iterations=2)
    assert names == ["mlx"]
    assert Child.launched[0][1]["env"]["JAIS_ASR_MODEL_DIR"] == str(tmp / "runtime/models/ASR")
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
    assert stopped.count(100001) >= 2  # SIGTERM then SIGKILL of its group on deletion


def test_supervisor_starts_installed_asr_first(store, monkeypatch):
    _, base, tmp = store
    install_asr(base)
    names, _ = run_supervisor(monkeypatch, tmp, iterations=1)
    assert names == ["asr", "mlx"]


def test_invalid_catalog_stops_before_any_service(store, monkeypatch):
    _, _, tmp = store
    model_store.CATALOG.write_text("{}")
    monkeypatch.setattr(run_services, "RUNTIME", tmp / "runtime")
    monkeypatch.setattr(run_services.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("no service may start"))
    with pytest.raises(ValueError, match="Invalid model catalog"):
        run_services.main()


def test_agent_launcher_uses_isolated_configuration(store, monkeypatch):
    _, _, tmp = store
    monkeypatch.setattr(run_agent, "RUNTIME", tmp / "runtime")
    argv, env, cwd = run_agent.command({"JAIS_NODE": run_agent.find_node({}), "JAIS_TOOLS_BIN": "/bundle/runtime/bin"})
    assert argv[argv.index("--mode") + 1] == "rpc" and "--no-session" in argv and "--no-context-files" in argv
    assert argv[argv.index("--no-extensions") + 1:argv.index("--no-extensions") + 5] == ["-e", "builtin:mcp", "-e", str(run_agent.ROOT / "agent/extensions/jais")]
    agent_dir = tmp / "runtime/pi-agent"
    assert env["PI_CODING_AGENT_DIR"] == str(agent_dir) and env["PI_OFFLINE"] == "1"
    assert all((agent_dir / name).read_text() == (run_agent.ROOT / "agent" / name).read_text() for name in run_agent.CONFIG_FILES)
    skills = sorted(path.relative_to(agent_dir) for path in (agent_dir / "skills").rglob("SKILL.md"))
    assert skills and skills == sorted(path.relative_to(run_agent.ROOT / "agent") for path in (run_agent.ROOT / "agent/skills").rglob("SKILL.md"))
    assert env["JAIS_AGENT_WORKSPACE"] == str(tmp / "runtime/agent-workspace") and (tmp / "runtime/agent-workspace").is_dir()
    # Pi's file tools resolve paths from the home folder; rg and fd come first on the PATH.
    assert cwd == Path.home() and env["PATH"].split(":")[0] == "/bundle/runtime/bin"


def test_agent_tools_are_pinned_builtins_without_bash_or_personal_skills():
    settings = json.loads((run_agent.ROOT / "agent/settings.json").read_text())
    assert settings["defaultTools"] == ["read", "write", "edit", "grep", "find", "ls"]
    assert "!**/.agents/**" in settings["skills"]
    from scripts import agent_tools
    assert set(agent_tools.TOOLS) == {"rg", "fd"} and all(len(digest) == 64 for _, digest, _ in agent_tools.TOOLS.values())


def test_pi_knows_every_catalog_model():
    settings = json.loads((run_agent.ROOT / "agent/settings.json").read_text())
    ids = {model["id"] for model in json.loads((run_agent.ROOT / "agent/models.json").read_text())["providers"]["jais"]["models"]}
    catalog = {entry["id"] for entry in model_store.load_catalog() if entry["kind"] == "llm"}
    assert ids == catalog and settings["defaultModel"] in ids
