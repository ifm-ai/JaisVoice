"""Small GPU conversion checks; no full model or downloaded Python is executed."""
import json
import numpy as np
import pytest
import mlx.core as mx
import mlx.nn as nn

from k2_runtime.model import Model, ModelArgs, checkpoint_weights, convert_checkpoint
from mlx.utils import tree_flatten


@pytest.mark.parametrize("bits", [None, 8])
def test_fp32_export_converts_and_preserves_baked_norms(tmp_path, bits):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    config = dict(hidden_size=128, intermediate_size=256, num_hidden_layers=1,
        num_attention_heads=4, num_key_value_heads=2, head_dim=64, vocab_size=128,
        layernorm_num_groups=2, rope_parameters={"rope_type": "default", "rope_theta": 10_000_000.},
        # Intentionally different from physical FP32, as in the real 4B export.
        dtype="bfloat16")
    model = Model(ModelArgs.from_dict(config))
    weights = dict(tree_flatten(model.parameters()))
    for name in weights:
        if "layernorm" in name or name == "model.norm.weight":
            weights[name] = mx.full(weights[name].shape, 1.375, dtype=mx.float32)
    shard = "model-00001-of-00001.safetensors"
    mx.save_safetensors(str(source / shard), weights)
    (source / "config.json").write_text(json.dumps(config))
    (source / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {name: shard for name in weights}}))
    convert_checkpoint(source, target, bits)
    converted = checkpoint_weights(target)
    result_config = json.loads((target / "config.json").read_text())
    assert result_config["dtype"] == "bfloat16"
    for name in ("model.norm.weight", "model.layers.0.input_layernorm.weight", "model.layers.0.post_attention_layernorm.weight"):
        assert converted[name].dtype == mx.bfloat16
        np.testing.assert_array_equal(np.array(converted[name].astype(mx.float32)), np.full(128, 1.375))
    name = "model.layers.0.self_attn.q_proj.weight"
    if bits:
        assert result_config["quantization"] == {"group_size": 64, "bits": 8, "mode": "affine"}
        assert converted[name].dtype == mx.uint32
        restored = mx.dequantize(converted[name], converted[name.removesuffix("weight") + "scales"],
                               converted[name.removesuffix("weight") + "biases"], group_size=64, bits=8)
        reference = weights[name].astype(mx.bfloat16).astype(mx.float32)
        relative = mx.sqrt(mx.mean(mx.square(restored.astype(mx.float32) - reference))) / mx.sqrt(mx.mean(mx.square(reference)))
        assert relative.item() < 0.015
    else:
        assert "quantization" not in result_config
        assert converted[name].dtype == mx.bfloat16
        np.testing.assert_array_equal(np.array(converted[name].astype(mx.float32)), np.array(weights[name].astype(mx.bfloat16).astype(mx.float32)))
    # Match production loading and ensure packed embeddings/head/projections fit.
    reloaded = Model(ModelArgs.from_dict(result_config))
    if bits:
        nn.quantize(reloaded, group_size=64, bits=8,
                    class_predicate=lambda path, module: path + ".scales" in converted)
    reloaded.load_weights(list(converted.items()), strict=True)
    logits = reloaded(mx.array([[1, 2, 3]]))
    assert logits.shape == (1, 3, 128)
    assert mx.all(mx.isfinite(logits)).item()
