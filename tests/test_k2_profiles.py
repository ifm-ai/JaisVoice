"""Model profile and native-prompt framing checks; no inference substitutions."""
import json

import pytest

from k2_runtime import gateway


@pytest.fixture
def profile(tmp_path):
    entries = {}
    for alias in gateway.MODEL_ALIASES:
        directory = tmp_path / alias
        directory.mkdir()
        (directory / "config.json").write_text('{}')
        entries[alias] = {"path": alias, "repo": "fixture/" + alias}
    path = tmp_path / "manifest-local.json"
    path.write_text(json.dumps(entries))
    return path, entries


def test_default_and_explicit_profile_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, "RUNTIME", tmp_path)
    monkeypatch.delenv("HORIZON_MODEL_MANIFEST", raising=False)
    assert gateway.model_manifest_path() == tmp_path / "models/manifest.json"
    selected = tmp_path / "models/manifest-mblab.json"
    monkeypatch.setenv("HORIZON_MODEL_MANIFEST", str(selected))
    assert gateway.model_manifest_path() == selected
    with pytest.raises(FileNotFoundError):
        gateway.read_model_manifest(selected)


def test_legacy_profile_defaults_and_relative_paths(profile):
    path, original = profile
    entries = gateway.read_model_manifest(path)
    assert entries["k2-cleaner"]["context_limit"] == 4096
    assert entries["k2-agent"]["context_limit"] == 16384
    assert entries["k2-agent"]["display_name"] == "fixture/k2-agent"
    assert entries["k2-agent"]["loader"] == "horizon-native-mlx-v1"
    assert entries["k2-cleaner"]["default_reasoning_effort"] == "low"
    assert entries["k2-agent"]["default_reasoning_effort"] == "medium"
    assert entries["k2-cleaner"]["path"] == str(path.parent / "k2-cleaner")
    assert json.loads(path.read_text()) == original


def test_explicit_profile_labels_context_and_options(profile):
    path, entries = profile
    entries["k2-agent"].update(display_name="MBZUAI · 4B", context_limit=8192,
                               reasoning_efforts=["medium"], tool_call_format="json")
    path.write_text(json.dumps(entries))
    loaded = gateway.read_model_manifest(path)["k2-agent"]
    assert loaded["display_name"] == "MBZUAI · 4B"
    assert loaded["context_limit"] == 8192
    assert loaded["reasoning_efforts"] == ["medium"]
    assert loaded["tool_call_format"] == "json"


@pytest.mark.parametrize("field,value", [
    ("path", ""), ("path", "missing"), ("display_name", " "),
    ("context_limit", True), ("context_limit", 0), ("context_limit", 9999999),
    ("loader", "arbitrary-code"), ("protocol", "guess"),
    ("reasoning_efforts", []), ("reasoning_efforts", ["none"]),
    ("reasoning_efforts", [{}]), ("tool_call_format", "python"),
    ("tool_presentation_format", "guess"),
    ("default_reasoning_effort", "none"), ("default_reasoning_effort", None),
])
def test_invalid_profile_is_rejected(profile, field, value):
    path, entries = profile
    entries["k2-agent"][field] = value
    path.write_text(json.dumps(entries))
    with pytest.raises(ValueError):
        gateway.read_model_manifest(path)


def test_incomplete_pair_rejected(profile):
    path, entries = profile
    del entries["k2-cleaner"]
    path.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match="k2-cleaner and k2-agent"):
        gateway.read_model_manifest(path)


def test_explicit_default_effort_must_be_supported(profile):
    path, entries = profile
    entries['k2-agent'].update(default_reasoning_effort='low', reasoning_efforts=['medium'])
    path.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match='default_reasoning_effort'):
        gateway.read_model_manifest(path)
    entries['k2-agent']['reasoning_efforts'] = ['low', 'medium']
    path.write_text(json.dumps(entries))
    assert gateway.read_model_manifest(path)['k2-agent']['default_reasoning_effort'] == 'low'


class FixtureTokenizer:
    """Only tests framing and option forwarding, never stands in for inference."""
    def __init__(self, suffix):
        self.rendered = '<|ifm|im_start|>user\nquestion<|ifm|im_end|><|ifm|im_start|>assistant\n' + suffix
        self.options = None

    def apply_chat_template(self, messages, **options):
        self.options = options
        return self.rendered

    def encode(self, rendered, *, add_special_tokens):
        assert add_special_tokens is False
        return list(rendered.encode())


@pytest.mark.parametrize("effort,tag", [("low", "think_faster"), ("medium", "think_fast"), ("high", "think")])
def test_open_reasoning_is_not_leaked(effort, tag):
    tokenizer = FixtureTokenizer(f'<ifm|{tag}>\n')
    tokens, parser = gateway.prepare_k2_prompt(tokenizer, [], [], effort, {})
    assert tokens == list(tokenizer.rendered.encode())
    assert parser.mode == "reasoning"
    assert parser.feed('private thought') == [{"reasoning_content": "private thought"}]
    assert parser.feed(f'</ifm|{tag}>answer') == [{"content": "answer"}]
    assert parser.finish('stop') == 'stop'
    assert tokenizer.options['reasoning_effort'] == effort


@pytest.mark.parametrize("trailing", ["", "\n"])
def test_preclosed_low_thinking_starts_with_content(trailing):
    tokenizer = FixtureTokenizer('<ifm|think_faster>\n</ifm|think_faster>' + trailing)
    _, parser = gateway.prepare_k2_prompt(tokenizer, [], [], 'low', {})
    assert parser.mode == "content"
    assert parser.feed('مرحباً') == [{"content": "مرحباً"}]
    assert parser.reasoning == ''
    assert parser.finish('stop') == 'stop'


def test_preclosed_low_keeps_structured_tools_out_of_content():
    tools = [{"type": "function", "function": {"name": "browser_snapshot", "parameters": {"type": "object", "properties": {}}}}]
    tokenizer = FixtureTokenizer('<ifm|think_faster>\n</ifm|think_faster>')
    _, parser = gateway.prepare_k2_prompt(tokenizer, [], tools, 'low', {})
    events = parser.feed('<ifm|tool_calls><ifm|tool_call>{"name":"browser_snapshot","arguments":{}}</ifm|tool_call></ifm|tool_calls>')
    assert parser.content == ''
    assert parser.finish('stop') == 'tool_calls'
    assert not any('content' in event for event in events)


@pytest.mark.parametrize("suffix", ['', '<think>', '<ifm|think_fast>\n', '<ifm|think_faster>\nprivate reasoning'])
def test_unknown_or_wrong_effort_prefill_is_rejected(suffix):
    with pytest.raises(ValueError, match="generation prefix"):
        gateway.prepare_k2_prompt(FixtureTokenizer(suffix), [], [], 'low', {})


def test_unsupported_effort_never_reaches_template():
    tokenizer = FixtureTokenizer('')
    with pytest.raises(ValueError, match="reasoning_effort"):
        gateway.prepare_k2_prompt(tokenizer, [], [], 'low', {"reasoning_efforts": ["medium"]})
    assert tokenizer.options is None


@pytest.mark.parametrize("directory", ["K2-Horizon-0.9B-bf16", "K2-Horizon-7B-8bit", "sources/mblab-1B-32bfb4a00c1e", "sources/mblab-4B-20e95a2e3f44"])
def test_installed_checkpoint_templates_and_tokenization(directory):
    """Optional local-artifact check, no download, GPU or inference required."""
    path = gateway.RUNTIME / "models" / directory
    if not (path / "tokenizer.json").exists():
        pytest.skip("Checkpoint tokenizer is not installed locally")
    from transformers import AutoTokenizer
    from k2_runtime.protocol import normalize_messages
    tokenizer = AutoTokenizer.from_pretrained(str(path), trust_remote_code=False, local_files_only=True)
    messages = normalize_messages([{"role": "user", "content": "Bonjour"}])
    for effort in ("low", "medium", "high"):
        tokens, parser = gateway.prepare_k2_prompt(tokenizer, messages, [], effort, {})
        expected = tokenizer.apply_chat_template(messages, tokenize=True, return_dict=False, add_generation_prompt=True,
            reasoning_effort=effort, tool_call_format='xml', tool_presentation_format='markdown')
        assert tokens == expected
        assert parser.mode == ("content" if directory.startswith("sources/mblab-") and effort == "low" else "reasoning")
