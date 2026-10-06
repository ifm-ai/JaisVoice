"""Shared test doubles: a temporary catalog and runtime, and tiny real model files."""
import json

import numpy as np
import pytest
from safetensors.numpy import save_file

from jais_runtime import model_store

REVISION = "b" * 40


def decoder_weights(config, quantized=False, prefix=""):
    weights = {}
    for name, shape in model_store.expected_weight_shapes(config).items():
        name = prefix + name
        if quantized and len(shape) == 2:
            group = config.get("quantization", {}).get("group_size", 64)
            weights[name] = np.zeros((shape[0], shape[1] // 4), dtype=np.uint32)
            weights[name.removesuffix(".weight") + ".scales"] = np.ones((shape[0], shape[1] // group), dtype=np.float32)
            weights[name.removesuffix(".weight") + ".biases"] = np.zeros((shape[0], shape[1] // group), dtype=np.float32)
        else:
            weights[name] = np.ones(shape, dtype=np.float32)
    return weights


def write_files(directory, config, weights):
    shard = "model-00001-of-00001.safetensors"
    save_file(weights, directory / shard)
    (directory / "config.json").write_text(json.dumps(config))
    (directory / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {name: shard for name in weights}}))
    (directory / "tokenizer.json").write_text('{"model": {"type": "WordLevel", "vocab": {"test": 0}}}')
    (directory / "tokenizer_config.json").write_text('{"tokenizer_class": "PreTrainedTokenizerFast"}')
    (directory / "chat_template.jinja").write_text('{{ messages }}')


def text_config(quantized=False):
    config = dict(model_type="k2_horizon", hidden_size=128, intermediate_size=256,
        num_attention_heads=2, num_key_value_heads=1, head_dim=64, vocab_size=128,
        num_hidden_layers=1, layernorm_num_groups=2, max_position_embeddings=16384)
    if quantized:
        config["quantization"] = {"bits": 8, "group_size": 64, "mode": "affine"}
    return config


def write_model(directory, quantized=False):
    """A complete, structurally valid model with one small layer."""
    directory.mkdir(parents=True)
    config = text_config(quantized)
    write_files(directory, config, decoder_weights(config, quantized))


def write_asr(directory):
    """A structurally valid Jais-ASR build: tiny encoder and projector, 8-bit decoder, processor files."""
    directory.mkdir(parents=True)
    quantization = {"bits": 8, "group_size": 32, "mode": "affine"}
    text = text_config()
    config = {"model_type": "jais_asr", "audio_token_id": 3, "text_config": text, "quantization": quantization,
              "audio_config": {"model_type": "qwen3_omni_moe_audio_encoder", "output_dim": 8}}
    weights = decoder_weights({**text, "quantization": quantization}, quantized=True, prefix="language_model.")
    weights["audio_tower.proj2.weight"] = np.ones((8, 8), dtype=np.float32)
    weights["multi_modal_projector.linear_1.weight"] = np.ones((128, 8), dtype=np.float32)
    write_files(directory, config, weights)
    for name in ("preprocessor_config.json", "processor_config.json", "generation_config.json"):
        (directory / name).write_text("{}")
    for name in model_store.ASR_CODE:
        (directory / name).write_text("# processor\n")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temporary catalog (ASR and the three sizes) and an empty runtime: (catalog, models dir, tmp)."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"models": [
        {"id": "jais-asr", "kind": "asr", "display_name": "ASR", "directory": "ASR",
         "download": {"repo": "org/asr", "revision": REVISION}},
        *({"id": "jais-3-" + size.lower(), "kind": "llm", "size": size, "display_name": "Test " + size,
           "directory": "model-" + size, "download": {"repo": "org/model-" + size, "revision": REVISION}}
          for size in model_store.SIZES),
    ]}))
    monkeypatch.setattr(model_store, "CATALOG", catalog)
    monkeypatch.setenv("JAIS_RUNTIME_DIR", str(tmp_path / "runtime"))
    return model_store.load_catalog(), tmp_path / "runtime/models", tmp_path
