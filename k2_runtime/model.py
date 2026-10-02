"""Dense K2 Horizon MLX implementation, matching the pinned IFM architecture.

Group RMS normalization is intentional: IFM's 7B uses FOUR groups and the
MBZUAI 4B uses TWO groups. Ordinary RMSNorm is not equivalent. RoPE honors
the configured base as well as IFM's cleaner YaRN attention factor and beta values.
Reference: IFM/modeling_k2_horizon.py and Transformers' YaRN specification.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
import shutil

import mlx.core as mx
import mlx.nn as nn
from mlx.utils import tree_flatten
from mlx_lm.models.base import create_attention_mask, scaled_dot_product_attention
from mlx_lm.models.cache import KVCache
from mlx_lm.tokenizer_utils import TokenizerWrapper
from transformers import AutoTokenizer

from k2_runtime.checkpoint import checkpoint_layout


@dataclass
class ModelArgs:
    hidden_size: int
    intermediate_size: int
    num_hidden_layers: int
    num_attention_heads: int
    num_key_value_heads: int
    head_dim: int
    vocab_size: int
    layernorm_num_groups: int = 1
    rms_norm_eps: float = 1e-6
    rope_theta: float = 1000000.0
    rope_parameters: dict | None = None
    tie_word_embeddings: bool = False
    attention_bias: bool = False
    model_type: str = "k2_horizon"

    @classmethod
    def from_dict(cls, config):
        if config.get("num_experts", 0) or config.get("mova_num_experts", 0) or config.get("query_key_norm") or config.get("attention_gate_func"):
            raise ValueError("Only dense, non-gated K2 checkpoints are supported")
        if config.get("hidden_act", "silu") != "silu" or config.get("use_sliding_window", False):
            raise ValueError("Only dense K2 SiLU/full-attention checkpoints are supported")
        if config.get("rope_head_dim", config["head_dim"]) != config["head_dim"]:
            raise ValueError("Partial RoPE is not present in supported K2 checkpoints")
        return cls(**{k: v for k, v in config.items() if k in {f.name for f in fields(cls)}})


class GroupRMSNorm(nn.Module):
    def __init__(self, dimensions: int, groups: int, eps: float):
        super().__init__()
        if dimensions % groups:
            raise ValueError("Group size must divide hidden dimension")
        self.weight = mx.ones(dimensions)
        self.groups = groups
        self.eps = eps

    def __call__(self, x):
        grouped = x.astype(mx.float32).reshape(*x.shape[:-1], self.groups, -1)
        normalized = grouped * mx.rsqrt(mx.mean(mx.square(grouped), axis=-1, keepdims=True) + self.eps)
        return (normalized.reshape(x.shape) * self.weight.astype(mx.float32)).astype(x.dtype)


class K2RoPE(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()
        p = args.rope_parameters or {}
        dimensions = args.head_dim
        base = p.get("rope_theta", args.rope_theta)
        inverse = 1.0 / (base ** (mx.arange(0, dimensions, 2, dtype=mx.float32) / dimensions))
        rope_type = p.get("rope_type", "default")
        self.attention_factor = 1.0
        if rope_type == "yarn":
            factor = p["factor"]
            original = p["original_max_position_embeddings"]
            correction = lambda rotations: dimensions * math.log(original / (rotations * 2 * math.pi)) / (2 * math.log(base))
            low, high = correction(p.get("beta_fast", 32)), correction(p.get("beta_slow", 1))
            if p.get("truncate", True):
                low, high = math.floor(low), math.ceil(high)
            low, high = max(low, 0), min(high, dimensions - 1)
            if low == high:
                high += 0.001
            extrapolate = 1 - mx.clip((mx.arange(dimensions // 2, dtype=mx.float32) - low) / (high - low), 0, 1)
            inverse = inverse / factor * (1 - extrapolate) + inverse * extrapolate
            self.attention_factor = p.get("attention_factor", 1 + 0.1 * math.log(factor))
        elif rope_type != "default":
            raise ValueError(f"Unsupported RoPE: {rope_type}")
        self._inverse = inverse

    def __call__(self, x, offset=0):
        # IFM forms cos/sin in float32, casts to input dtype, then rotates halves.
        positions = mx.arange(offset, offset + x.shape[-2], dtype=mx.float32)
        phases = positions[:, None] * self._inverse[None, :]
        phases = mx.concatenate([phases, phases], axis=-1)
        cos = (mx.cos(phases) * self.attention_factor).astype(x.dtype)
        sin = (mx.sin(phases) * self.attention_factor).astype(x.dtype)
        half = x.shape[-1] // 2
        rotated = mx.concatenate([-x[..., half:], x[..., :half]], axis=-1)
        return x * cos + rotated * sin


class Attention(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.n_heads, self.n_kv_heads, self.head_dim = args.num_attention_heads, args.num_key_value_heads, args.head_dim
        self.q_proj = nn.Linear(args.hidden_size, self.n_heads * self.head_dim, bias=args.attention_bias)
        self.k_proj = nn.Linear(args.hidden_size, self.n_kv_heads * self.head_dim, bias=args.attention_bias)
        self.v_proj = nn.Linear(args.hidden_size, self.n_kv_heads * self.head_dim, bias=args.attention_bias)
        self.o_proj = nn.Linear(self.n_heads * self.head_dim, args.hidden_size, bias=args.attention_bias)
        self.rope = K2RoPE(args)

    def __call__(self, x, mask, cache):
        batch, length, _ = x.shape
        q = self.q_proj(x).reshape(batch, length, self.n_heads, self.head_dim).transpose(0, 2, 1, 3)
        k = self.k_proj(x).reshape(batch, length, self.n_kv_heads, self.head_dim).transpose(0, 2, 1, 3)
        v = self.v_proj(x).reshape(batch, length, self.n_kv_heads, self.head_dim).transpose(0, 2, 1, 3)
        offset = cache.offset if cache is not None else 0
        q, k = self.rope(q, offset), self.rope(k, offset)
        if cache is not None:
            k, v = cache.update_and_fetch(k, v)
        out = scaled_dot_product_attention(q, k, v, cache=cache, scale=self.head_dim ** -0.5, mask=mask)
        return self.o_proj(out.transpose(0, 2, 1, 3).reshape(batch, length, -1))


class MLP(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.gate_proj = nn.Linear(args.hidden_size, args.intermediate_size, bias=False)
        self.up_proj = nn.Linear(args.hidden_size, args.intermediate_size, bias=False)
        self.down_proj = nn.Linear(args.intermediate_size, args.hidden_size, bias=False)

    def __call__(self, x):
        return self.down_proj(nn.silu(self.gate_proj(x)) * self.up_proj(x))


class Block(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.self_attn = Attention(args)
        self.mlp = MLP(args)
        self.input_layernorm = GroupRMSNorm(args.hidden_size, args.layernorm_num_groups, args.rms_norm_eps)
        self.post_attention_layernorm = GroupRMSNorm(args.hidden_size, args.layernorm_num_groups, args.rms_norm_eps)

    def __call__(self, x, mask, cache):
        h = x + self.self_attn(self.input_layernorm(x), mask, cache)
        return h + self.mlp(self.post_attention_layernorm(h))


class Backbone(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.embed_tokens = nn.Embedding(args.vocab_size, args.hidden_size)
        self.layers = [Block(args) for _ in range(args.num_hidden_layers)]
        self.norm = GroupRMSNorm(args.hidden_size, args.layernorm_num_groups, args.rms_norm_eps)

    def __call__(self, inputs, cache=None):
        x = self.embed_tokens(inputs)
        if cache is None:
            cache = [None] * len(self.layers)
        mask = create_attention_mask(x, cache[0])
        for layer, layer_cache in zip(self.layers, cache):
            x = layer(x, mask, layer_cache)
        return self.norm(x)


class Model(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args, self.model_type = args, args.model_type
        self.model = Backbone(args)
        if not args.tie_word_embeddings:
            self.lm_head = nn.Linear(args.hidden_size, args.vocab_size, bias=False)

    @property
    def layers(self):
        return self.model.layers

    def make_cache(self):
        return [KVCache() for _ in self.layers]

    def __call__(self, inputs, cache=None):
        h = self.model(inputs, cache)
        return self.model.embed_tokens.as_linear(h) if self.args.tie_word_embeddings else self.lm_head(h)


def checkpoint_weights(path: Path):
    index = path / "model.safetensors.index.json"
    names = sorted(set(json.loads(index.read_text())["weight_map"].values())) if index.exists() else sorted(p.name for p in path.glob("*.safetensors"))
    if not names:
        raise FileNotFoundError(f"No safetensors checkpoint in {path}")
    weights = {}
    for filename in names:
        weights.update(mx.load(str(path / filename)))
    return weights


def load_model(path: Path):
    config = json.loads((path / "config.json").read_text())
    model = Model(ModelArgs.from_dict(config))
    weights = checkpoint_weights(path)
    quantization = config.get("quantization")
    if quantization:
        nn.quantize(model, group_size=quantization["group_size"], bits=quantization["bits"],
                    class_predicate=lambda name, module: name + ".scales" in weights)
    model.load_weights(list(weights.items()), strict=True)
    model.eval()
    mx.eval(model.parameters())
    tokenizer = TokenizerWrapper(AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False))
    for token in ("<|ifm|im_end|>", "<|ifm|endoftext|>", "<|endoftext|>"):
        if token in tokenizer.get_vocab():
            tokenizer.add_eos_token(token)
    return model, tokenizer, config


def convert_checkpoint(source: Path, target: Path, bits=None):
    """Convert shards independently, explicitly rounding stored source values to BF16.

    Source config dtype is not trusted: these HF exports may physically store FP32.
    Norm weights are already baked HF values; NEVER add a native-XLLM offset here.
    """
    if bits not in (None, 8):
        raise ValueError("Supported conversion precisions are BF16 and 8-bit affine group64")
    checkpoint_layout(source)
    config = json.loads((source / "config.json").read_text())
    ModelArgs.from_dict(config)
    index = json.loads((source / "model.safetensors.index.json").read_text())
    weight_map, total = {}, 0
    shards = sorted(set(index["weight_map"].values()))
    for n, filename in enumerate(shards, 1):
        converted = {}
        for name, weight in mx.load(str(source / filename)).items():
            weight = weight.astype(mx.bfloat16)
            if bits and weight.ndim == 2 and name.endswith(".weight") and weight.shape[-1] % 64 == 0:
                packed, scales, biases = mx.quantize(weight, group_size=64, bits=bits)
                base = name.removesuffix(".weight")
                converted.update({name: packed, base + ".scales": scales, base + ".biases": biases})
            else:
                converted[name] = weight
        mx.eval(converted)
        shard = f"model-{n:05d}-of-{len(shards):05d}.safetensors"
        mx.save_safetensors(str(target / shard), converted, metadata={"format": "mlx"})
        weight_map.update({name: shard for name in converted})
        total += sum(w.nbytes for w in converted.values())
        del converted
        mx.clear_cache()
        print(f"Converted shard {n}/{len(shards)}", flush=True)
    config["dtype"] = "bfloat16"
    if bits:
        config["quantization"] = {"group_size": 64, "bits": bits, "mode": "affine"}
    (target / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (target / "model.safetensors.index.json").write_text(json.dumps({"metadata": {"total_size": total}, "weight_map": weight_map}, indent=2) + "\n")
    for filename in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "chat_template.jinja", "generation_config.json"):
        if (source / filename).exists():
            shutil.copy2(source / filename, target / filename)
