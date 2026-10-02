"""Shared catalog/inventory doubles for gateway tests; no weights or Metal."""
from k2_runtime import model_store


def llm_entry(size, path, *, installed=True, **overrides):
    entry = {"id": "k2-" + size.lower(), "kind": "llm", "size": size, "display_name": "Test " + size,
             "directory": "model-" + size, "download": None, "path": str(path), "installed": installed,
             **model_store.LLM_DEFAULTS}
    entry.update(overrides)
    return entry


def inventory(tmp_path, sizes=("0.9B", "3.7B"), **overrides):
    return {size: llm_entry(size, tmp_path / size, **overrides) for size in sizes}


def loaded(sizes=("0.9B", "3.7B"), max_positions=32768):
    """worker.models doubles: (model, tokenizer, config, entry) per size."""
    return {size: (object(), object(), {"max_position_embeddings": max_positions},
                   {"size": size, "display_name": "Test " + size, "context_limit": 16384,
                    "cleaner_reasoning_effort": "low", "agent_reasoning_effort": "low"})
            for size in sizes}
