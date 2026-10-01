"""Model catalog and native-prompt framing checks; no inference substitutions."""
import json

import pytest

from k2_runtime import gateway, model_store


def write_catalog(tmp_path, models):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"format": "jais-voice-models-v1", "models": models}))
    return path


def llm(size="3.7B", **fields):
    return {"id": "k2-" + size.lower(), "kind": "llm", "size": size, "display_name": "Test " + size,
            "directory": "model-" + size, "download": None, **fields}


ASR = {"id": "asr-f5", "kind": "asr", "display_name": "ASR", "directory": "ASR",
       "download": {"repo": "org/asr", "revision": "a" * 40, "profile": "F5", "bytes": 10}}


def test_shipped_catalogs_are_valid():
    for name in ("catalog.json", "catalog-ifm.json"):
        entries = model_store.load_catalog(model_store.ROOT / "models" / name)
        assert [entry["kind"] for entry in entries].count("asr") == 1
    sizes = [entry["size"] for entry in model_store.load_catalog(model_store.ROOT / "models/catalog.json") if entry["kind"] == "llm"]
    assert sizes == ["0.9B", "3.7B", "7B"]


def test_catalog_defaults_and_override(monkeypatch, tmp_path):
    path = write_catalog(tmp_path, [ASR, llm(), llm("0.9B", agent_reasoning_effort="medium", context_limit=8192)])
    monkeypatch.setenv("HORIZON_MODEL_CATALOG", str(path))
    assert model_store.catalog_path() == path
    entries = {entry["id"]: entry for entry in model_store.load_catalog()}
    assert entries["k2-3.7b"]["context_limit"] == 16384 and entries["k2-3.7b"]["agent_reasoning_effort"] == "low"
    assert entries["k2-0.9b"]["agent_reasoning_effort"] == "medium" and entries["k2-0.9b"]["context_limit"] == 8192
    assert entries["asr-f5"]["download"] == {"repo": "org/asr", "revision": "a" * 40, "profile": "F5", "bytes": 10}
    monkeypatch.delenv("HORIZON_MODEL_CATALOG")
    assert model_store.catalog_path() == model_store.ROOT / "models/catalog.json"


@pytest.mark.parametrize("field,value", [
    ("size", "13B"), ("directory", "../escape"), ("directory", ".hidden"), ("display_name", " "),
    ("context_limit", True), ("context_limit", 0), ("context_limit", 9999999),
    ("loader", "arbitrary-code"), ("protocol", "guess"),
    ("reasoning_efforts", []), ("reasoning_efforts", ["none"]), ("tool_call_format", "python"),
    ("tool_presentation_format", "guess"), ("agent_reasoning_effort", "none"),
    ("download", {"repo": "org/model", "revision": "main"}),
    ("download", {"repo": "not a repo", "revision": "a" * 40}),
    ("download", {"repo": "org/model", "revision": "a" * 40, "bytes": -1}),
])
def test_invalid_catalog_entry_rejects_the_catalog(tmp_path, field, value):
    with pytest.raises(ValueError):
        model_store.load_catalog(write_catalog(tmp_path, [llm(**{field: value})]))


@pytest.mark.parametrize("models", [
    [llm(), llm()], [llm("0.9B"), llm("0.9B", id="other", directory="other")], [ASR, dict(ASR, id="asr-2", directory="B")], [ASR],
])
def test_duplicates_and_missing_language_models_are_rejected(tmp_path, models):
    with pytest.raises(ValueError):
        model_store.load_catalog(write_catalog(tmp_path, models))


def test_default_effort_must_be_supported(tmp_path):
    with pytest.raises(ValueError, match="agent_reasoning_effort"):
        model_store.load_catalog(write_catalog(tmp_path, [llm(reasoning_efforts=["medium"], cleaner_reasoning_effort="medium")]))


def test_inventory_follows_directories_on_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("HORIZON_MODEL_CATALOG", str(write_catalog(tmp_path, [llm("0.9B"), llm()])))
    monkeypatch.setattr(gateway, "RUNTIME", tmp_path)
    assert {size: entry["installed"] for size, entry in gateway.read_inventory().items()} == {"0.9B": False, "3.7B": False}
    (tmp_path / "models/model-3.7B").mkdir(parents=True)
    (tmp_path / "models/model-3.7B/config.json").write_text("{}")
    inventory = gateway.read_inventory()
    assert inventory["3.7B"]["installed"] and inventory["3.7B"]["path"] == str(tmp_path / "models/model-3.7B")
    monkeypatch.setenv("HORIZON_AGENT_REASONING_EFFORT", "high")
    assert gateway.role_effort(inventory["3.7B"], "k2-agent") == "high"
    assert gateway.role_effort(inventory["3.7B"], "k2-cleaner") == "low"


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
