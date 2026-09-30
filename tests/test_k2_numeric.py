"""Independent NumPy checks of the specific IFM operations MLX ports miss."""
import math
import numpy as np
import pytest
import mlx.core as mx

from k2_runtime.model import GroupRMSNorm, K2RoPE, Model, ModelArgs


def args(**kwargs):
    base = dict(hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=2, head_dim=16, vocab_size=128,
        layernorm_num_groups=4)
    base.update(kwargs)
    return ModelArgs(**base)


@pytest.mark.parametrize('groups,dimensions', [(1, 4096), (2, 2560), (4, 4096)])
def test_group_rmsnorm_matches_official_float32_formula(groups, dimensions):
    rng = np.random.default_rng(714)
    x = rng.normal(size=(2, 7, dimensions)).astype(np.float32)
    # Four different scales expose accidental ordinary RMSNorm.
    x *= np.repeat(np.array([.1, 1., 10., 100.], dtype=np.float32), dimensions // 4)
    weight = rng.uniform(.5, 1.5, dimensions).astype(np.float32)
    grouped = x.reshape(2, 7, groups, -1)
    expected = (grouped / np.sqrt(np.mean(grouped ** 2, axis=-1, keepdims=True) + 1e-6)).reshape(x.shape) * weight
    norm = GroupRMSNorm(dimensions, groups, 1e-6)
    norm.weight = mx.array(weight)
    actual = np.array(norm(mx.array(x)))
    np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-6)


@pytest.mark.parametrize('yarn,base', [(False, 1_000_000.), (False, 10_000_000.), (True, 1_000_000.)])
def test_rope_matches_reference_at_offsets(yarn, base):
    dims, original = 64, 8192
    p = dict(rope_type='yarn', factor=16., original_max_position_embeddings=original,
             beta_fast=128., beta_slow=4., attention_factor=1.2772588722239782, truncate=True) if yarn else {'rope_type': 'default'}
    rope = K2RoPE(args(head_dim=dims, rope_theta=base, rope_parameters=p))
    inv = 1 / (base ** (np.arange(0, dims, 2, dtype=np.float32) / dims))
    factor = 1
    if yarn:
        correct = lambda r: dims * math.log(original / (r * 2 * math.pi)) / (2 * math.log(base))
        low, high = max(math.floor(correct(128)), 0), min(math.ceil(correct(4)), dims - 1)
        ramp = 1 - np.clip((np.arange(dims // 2) - low) / (high - low), 0, 1)
        inv = (inv / 16 * (1 - ramp) + inv * ramp).astype(np.float32)
        factor = p['attention_factor']
    x = np.random.default_rng(7).normal(size=(1, 4, 3, dims)).astype(np.float32)
    for offset in [0, 8191, 16300]:
        angles = np.arange(offset, offset + 3, dtype=np.float32)[:, None] * inv[None, :]
        angles = np.concatenate([angles, angles], axis=-1)
        rotated = np.concatenate([-x[..., dims//2:], x[..., :dims//2]], axis=-1)
        expected = x * (np.cos(angles) * factor) + rotated * (np.sin(angles) * factor)
        actual = np.array(rope(mx.array(x), offset))
        np.testing.assert_allclose(actual, expected, rtol=3e-3, atol=3e-3)


def test_cached_incremental_logits_match_whole_prompt():
    mx.random.seed(13)
    model = Model(args())
    tokens = mx.array([[0, 2, 3, 4, 7]])
    full = model(tokens)
    cache = model.make_cache()
    pieces = [model(tokens[:, i:i+1], cache=cache) for i in range(tokens.shape[1])]
    joined = mx.concatenate(pieces, axis=1)
    np.testing.assert_allclose(np.array(full), np.array(joined), rtol=1e-4, atol=1e-5)
