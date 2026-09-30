"""Persistent launch selection with tiny real safetensors, never real activation."""
import json

import numpy as np
import pytest
from safetensors.numpy import save_file

from k2_runtime.checkpoint import expected_weight_shapes
from scripts import run_services
from scripts import select_model_profile as profiles
from scripts.prepare_mblab_models import MODELS as MBLAB_MODELS


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
def installed(tmp_path):
    runtime = tmp_path / "runtime"
    models = runtime / "models"
    write_model(models / "cleaner")
    write_model(models / "agent", quantized=True)
    entries = {"k2-cleaner": {"path": "cleaner"}, "k2-agent": {"path": "agent"}}
    default = models / "manifest.json"
    alternative = models / "manifest-mblab.json"
    default.write_text(json.dumps(entries))
    alternative.write_text(json.dumps({alias: {**entry, "display_name": alias + " alternative"} for alias, entry in entries.items()}))
    return runtime, default, alternative


def test_no_selection_preserves_ifm_default(installed):
    runtime, default, _ = installed
    assert profiles.resolve_model_profile(runtime, {}) == default
    assert not profiles.selection_path(runtime).exists()


def test_legacy_and_mblab_profile_reasoning_defaults(installed):
    _, default, alternative = installed
    legacy = profiles.read_model_manifest(default)
    env = {}
    run_services.apply_profile_reasoning(env, legacy)
    assert env == {'HORIZON_CLEANER_REASONING_EFFORT': 'low', 'HORIZON_AGENT_REASONING_EFFORT': 'medium'}
    entries = json.loads(alternative.read_text())
    for alias in entries:
        entries[alias]['default_reasoning_effort'] = MBLAB_MODELS[alias]['default_reasoning_effort']
    alternative.write_text(json.dumps(entries))
    env = {}
    run_services.apply_profile_reasoning(env, profiles.read_model_manifest(alternative))
    assert env == {'HORIZON_CLEANER_REASONING_EFFORT': 'low', 'HORIZON_AGENT_REASONING_EFFORT': 'low'}


@pytest.mark.parametrize('variable', ['HORIZON_CLEANER_REASONING_EFFORT', 'HORIZON_AGENT_REASONING_EFFORT'])
def test_explicit_reasoning_override_takes_precedence(installed, variable):
    _, default, _ = installed
    env = {variable: 'high'}
    run_services.apply_profile_reasoning(env, profiles.read_model_manifest(default))
    assert env[variable] == 'high'


@pytest.mark.parametrize('effort', ['', 'none', 'invalid'])
def test_unsupported_reasoning_override_is_not_silently_replaced(installed, effort):
    _, default, _ = installed
    with pytest.raises(ValueError, match='HORIZON_AGENT_REASONING_EFFORT'):
        run_services.apply_profile_reasoning({'HORIZON_AGENT_REASONING_EFFORT': effort}, profiles.read_model_manifest(default))


def test_persisted_profile_is_atomic_and_does_not_rewrite_ifm(installed):
    runtime, default, alternative = installed
    original = default.read_bytes()
    assert profiles.select_profile(runtime, alternative) == alternative
    assert profiles.resolve_model_profile(runtime, {}) == alternative
    assert json.loads(profiles.selection_path(runtime).read_text())["manifest"] == "manifest-mblab.json"
    assert default.read_bytes() == original
    assert not list(default.parent.glob('.active-profile-*.tmp'))


def test_environment_override_wins_over_broken_persistent_selection(installed):
    runtime, default, _ = installed
    profiles.selection_path(runtime).write_text('{broken selector')
    assert profiles.resolve_model_profile(runtime, {"HORIZON_MODEL_MANIFEST": str(default)}) == default


def test_missing_environment_override_never_falls_back(installed):
    runtime, _, alternative = installed
    profiles.select_profile(runtime, alternative)
    with pytest.raises(ValueError, match="Profil de modèles invalide"):
        profiles.resolve_model_profile(runtime, {"HORIZON_MODEL_MANIFEST": str(alternative.parent / 'missing.json')})
    with pytest.raises(ValueError, match="vide"):
        profiles.resolve_model_profile(runtime, {"HORIZON_MODEL_MANIFEST": " "})


@pytest.mark.parametrize("document", ['{invalid json', '[]', '{"version": 1}', '{"version": 2, "manifest": "manifest.json"}', '{"version": true, "manifest": "manifest.json"}'])
def test_broken_selector_is_an_error(installed, document):
    runtime, _, _ = installed
    profiles.selection_path(runtime).write_text(document)
    with pytest.raises(ValueError, match="Sélection de modèles illisible"):
        profiles.resolve_model_profile(runtime, {})


def test_invalid_pair_cannot_replace_previous_selection(installed):
    runtime, default, alternative = installed
    profiles.select_profile(runtime, default)
    previous = profiles.selection_path(runtime).read_bytes()
    alternative.write_text(json.dumps({"k2-cleaner": {"path": "cleaner"}}))
    with pytest.raises(ValueError):
        profiles.select_profile(runtime, alternative)
    assert profiles.selection_path(runtime).read_bytes() == previous


def test_model_deleted_after_activation_is_reported(installed):
    runtime, _, alternative = installed
    profiles.select_profile(runtime, alternative)
    (runtime / 'models/agent/model-00001-of-00001.safetensors').unlink()
    with pytest.raises(ValueError, match="Shard absent"):
        profiles.resolve_model_profile(runtime, {})


def test_truncated_weight_payload_cannot_be_selected(installed):
    runtime, _, alternative = installed
    shard = runtime / 'models/agent/model-00001-of-00001.safetensors'
    shard.write_bytes(shard.read_bytes()[:-1])
    with pytest.raises(ValueError, match="Profil de modèles invalide"):
        profiles.select_profile(runtime, alternative)
    assert not profiles.selection_path(runtime).exists()


def test_tensor_shape_mismatch_is_rejected(installed):
    runtime, _, alternative = installed
    path = runtime / 'models/cleaner/config.json'
    config = json.loads(path.read_text())
    config['vocab_size'] += 1
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Forme ou type"):
        profiles.select_profile(runtime, alternative)


def test_index_cannot_escape_model_directory(installed):
    runtime, _, alternative = installed
    path = runtime / 'models/cleaner/model.safetensors.index.json'
    index = json.loads(path.read_text())
    index['weight_map'] = {name: '../agent/model-00001-of-00001.safetensors' for name in index['weight_map']}
    path.write_text(json.dumps(index))
    with pytest.raises(ValueError, match="Nom de shard"):
        profiles.select_profile(runtime, alternative)


def test_reset_removes_only_selector(installed):
    runtime, default, alternative = installed
    profiles.select_profile(runtime, alternative)
    before = {path: path.read_bytes() for path in default.parent.rglob('*') if path.is_file() and path != profiles.selection_path(runtime)}
    profiles.reset_profile(runtime)
    profiles.reset_profile(runtime)
    assert not profiles.selection_path(runtime).exists()
    assert profiles.resolve_model_profile(runtime, {}) == default
    assert all(path.read_bytes() == content for path, content in before.items())


def test_failed_atomic_replace_preserves_prior_selection(installed, monkeypatch):
    runtime, default, alternative = installed
    profiles.select_profile(runtime, default)
    original = profiles.selection_path(runtime).read_bytes()
    def fail_replace(source, destination):
        raise OSError('simulated write failure')
    monkeypatch.setattr(profiles.os, 'replace', fail_replace)
    with pytest.raises(OSError, match="write failure"):
        profiles.select_profile(runtime, alternative)
    assert profiles.selection_path(runtime).read_bytes() == original
    assert not list(default.parent.glob('.active-profile-*.tmp'))


def test_cli_select_and_reset_in_temporary_runtime(installed, capsys):
    runtime, _, alternative = installed
    assert profiles.main(['--runtime-dir', str(runtime), '--manifest', str(alternative)]) == 0
    assert 'prochain lancement' in capsys.readouterr().out
    assert profiles.main(['--runtime-dir', str(runtime), '--reset']) == 0
    assert 'Aucun modèle supprimé' in capsys.readouterr().out


def test_launcher_rejects_broken_selection_before_starting_services(installed, monkeypatch):
    runtime, _, _ = installed
    monkeypatch.setattr(run_services, 'RUNTIME', runtime)
    monkeypatch.delenv('HORIZON_MODEL_MANIFEST', raising=False)
    profiles.selection_path(runtime).write_text('{invalid selector')
    launched = []
    monkeypatch.setattr(run_services.subprocess, 'Popen', lambda *args, **kwargs: launched.append(args))
    with pytest.raises(ValueError, match="Sélection de modèles illisible"):
        run_services.main()
    assert launched == []


def test_launcher_forwards_persistent_profile_to_every_owned_service(installed, monkeypatch):
    runtime, _, alternative = installed
    entries = json.loads(alternative.read_text())
    entries['k2-agent']['default_reasoning_effort'] = 'low'
    alternative.write_text(json.dumps(entries))
    profiles.select_profile(runtime, alternative)
    monkeypatch.setattr(run_services, 'RUNTIME', runtime)
    monkeypatch.delenv('HORIZON_MODEL_MANIFEST', raising=False)
    monkeypatch.delenv('HORIZON_CLEANER_REASONING_EFFORT', raising=False)
    monkeypatch.delenv('HORIZON_AGENT_REASONING_EFFORT', raising=False)
    monkeypatch.setenv('HORIZON_NODE', '/explicit-test-node')
    launched, stopped = [], []
    class Child:
        def __init__(self, command, **kwargs):
            self.pid = 100000 + len(launched)
            launched.append((command, kwargs))
        def poll(self):
            return None
        def wait(self, **kwargs):
            return 0
    class Probe:
        count = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def connect_ex(self, address):
            Probe.count += 1
            return 1 if Probe.count <= 5 else 0
    class StopAfterLaunch:
        def is_set(self):
            return False
        def wait(self, timeout):
            return True
    monkeypatch.setattr(run_services.subprocess, 'Popen', Child)
    monkeypatch.setattr(run_services.socket, 'socket', Probe)
    monkeypatch.setattr(run_services.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(run_services.os, 'killpg', lambda pid, signal: stopped.append((pid, signal)))
    monkeypatch.setattr(run_services, 'STOP', StopAfterLaunch())
    assert run_services.main() == 0
    assert len(launched) == 5
    assert all(kwargs['env']['HORIZON_MODEL_MANIFEST'] == str(alternative) for _, kwargs in launched)
    assert all(kwargs['env']['HORIZON_CLEANER_REASONING_EFFORT'] == 'low' for _, kwargs in launched)
    assert all(kwargs['env']['HORIZON_AGENT_REASONING_EFFORT'] == 'low' for _, kwargs in launched)
    assert stopped == [(pid, sig) for sig in (run_services.signal.SIGTERM, run_services.signal.SIGKILL) for pid in reversed([100000 + i for i in range(5)])]
    assert not (runtime / 'services.json').exists()
