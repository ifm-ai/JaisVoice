"""Read and validate K2 safetensors without importing MLX or remote Python code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_weight_shapes(config: dict) -> dict[str, tuple[int, ...]]:
    hidden, intermediate = config["hidden_size"], config["intermediate_size"]
    query = config["num_attention_heads"] * config["head_dim"]
    key = config["num_key_value_heads"] * config["head_dim"]
    result = {"model.embed_tokens.weight": (config["vocab_size"], hidden), "model.norm.weight": (hidden,)}
    if not config.get("tie_word_embeddings", False):
        result["lm_head.weight"] = (config["vocab_size"], hidden)
    for n in range(config["num_hidden_layers"]):
        prefix = f"model.layers.{n}."
        for name, shape in {
            "self_attn.q_proj.weight": (query, hidden),
            "self_attn.k_proj.weight": (key, hidden),
            "self_attn.v_proj.weight": (key, hidden),
            "self_attn.o_proj.weight": (hidden, query),
            "mlp.gate_proj.weight": (intermediate, hidden),
            "mlp.up_proj.weight": (intermediate, hidden),
            "mlp.down_proj.weight": (hidden, intermediate),
            "input_layernorm.weight": (hidden,),
            "post_attention_layernorm.weight": (hidden,),
        }.items():
            result[prefix + name] = shape
        if config.get("attention_bias", False):
            for projection, dimension in (("q", query), ("k", key), ("v", key), ("o", hidden)):
                result[prefix + f"self_attn.{projection}_proj.bias"] = (dimension,)
    return result


def checkpoint_layout(source: Path, *, check_shapes: bool = True) -> dict:
    """Reject incomplete, duplicate, out-of-directory, or misindexed source tensors."""
    source = source.resolve()
    config = json.loads((source / "config.json").read_text())
    index = json.loads((source / "model.safetensors.index.json").read_text())
    weight_map = index["weight_map"]
    if not isinstance(weight_map, dict) or not weight_map:
        raise ValueError("The checkpoint weight index is empty")
    seen, element_counts, files = {}, {}, {}
    for filename in sorted(set(weight_map.values())):
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".safetensors"):
            raise ValueError(f"Unsafe checkpoint shard name: {filename!r}")
        path = source / filename
        if path.resolve().parent != source:
            raise ValueError(f"Checkpoint shard escapes its source directory: {filename}")
        size = path.stat().st_size
        with path.open("rb") as stream:
            raw_length = stream.read(8)
            if len(raw_length) != 8:
                raise ValueError(f"Truncated safetensors header: {filename}")
            length = struct.unpack("<Q", raw_length)[0]
            if not 2 <= length <= min(size - 8, 100 * 1024 * 1024):
                raise ValueError(f"Invalid safetensors header length: {filename}")
            header = json.loads(stream.read(length))
        end, count = 0, 0
        tensors = {k: v for k, v in header.items() if k != "__metadata__"}
        if not tensors:
            raise ValueError(f"Empty checkpoint shard: {filename}")
        for name, tensor in sorted(tensors.items(), key=lambda pair: pair[1]["data_offsets"][0]):
            if name in seen:
                raise ValueError(f"Duplicate tensor: {name}")
            if weight_map.get(name) != filename:
                raise ValueError(f"Weight index differs from shard contents: {name}")
            dtype = tensor["dtype"]
            if dtype not in {"F32", "BF16", "F16"}:
                raise ValueError(f"Unexpected source dtype {dtype} for {name}")
            shape = tensor["shape"]
            if not shape or any(not isinstance(d, int) or isinstance(d, bool) or d <= 0 for d in shape):
                raise ValueError(f"Invalid tensor shape: {name}")
            elements = 1
            for dimension in shape:
                elements *= dimension
            begin, finish = tensor["data_offsets"]
            if begin != end or finish - begin != elements * (4 if dtype == "F32" else 2):
                raise ValueError(f"Invalid tensor byte span: {name}")
            end = finish
            count += 1
            element_counts[dtype] = element_counts.get(dtype, 0) + elements
            seen[name] = tuple(shape)
        if end != size - 8 - length:
            raise ValueError(f"Truncated or extra safetensors payload: {filename}")
        files[filename] = {"bytes": size, "tensors": count}
    if set(seen) != set(weight_map):
        raise ValueError(f"Missing indexed tensors: {sorted(set(weight_map) - set(seen))[:3]}")
    if check_shapes:
        expected = expected_weight_shapes(config)
        if set(seen) != set(expected):
            raise ValueError(f"Architecture tensor names mismatch: missing={sorted(set(expected)-set(seen))[:3]}, extra={sorted(set(seen)-set(expected))[:3]}")
        for name, shape in expected.items():
            if seen[name] != shape:
                raise ValueError(f"Architecture tensor shape mismatch: {name}: {seen[name]} != {shape}")
    return {"tensor_count": len(seen), "elements_by_dtype": element_counts,
            "parameter_elements": sum(element_counts.values()), "files": files}


def read_expected_hashes(path: Path) -> dict[str, str]:
    document = json.loads(path.read_text())
    if "file_details" in document:
        entries = {entry["file"]: entry["sha256"] for entry in document["file_details"] if entry.get("sha256")}
    elif isinstance(document.get("files"), dict):
        entries = document["files"]
    else:
        raise ValueError(f"No explicit file SHA256 values in {path}")
    for name, digest in entries.items():
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
            raise ValueError(f"Invalid expected SHA256 for {name}")
    return {name: digest.lower() for name, digest in entries.items()}


def verify_source(source: Path, hashes_path: Path) -> dict:
    layout = checkpoint_layout(source)
    expected = read_expected_hashes(hashes_path)
    for filename, details in layout["files"].items():
        if filename not in expected:
            raise ValueError(f"Missing independent SHA256 for weight shard {filename}")
        digest = sha256_file(source / filename)
        if digest != expected[filename]:
            raise ValueError(f"Source SHA256 mismatch: {filename}")
        details["sha256"] = digest
    # Capture and, where supplied, compare every runtime-relevant metadata file.
    for filename in ("config.json", "model.safetensors.index.json", "tokenizer.json", "tokenizer_config.json",
                     "chat_template.jinja", "generation_config.json", "modeling_k2_horizon.py", "offset_fix_baked_norms.json",
                     "k2horCT_provenance.json", "k2horCT_export_verification.json", "publish_manifest.json"):
        path = source / filename
        if not path.exists():
            raise FileNotFoundError(f"Required source metadata missing: {path}")
        digest = sha256_file(path)
        if filename in expected and digest != expected[filename]:
            raise ValueError(f"Source SHA256 mismatch: {filename}")
        layout["files"][filename] = {"bytes": path.stat().st_size, "sha256": digest,
                                      "expected_sha256_matched": filename in expected}
    return layout
