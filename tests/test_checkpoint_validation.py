import json
from pathlib import Path
import struct

import pytest

from k2_runtime.checkpoint import checkpoint_layout, expected_weight_shapes, read_expected_hashes, sha256_file, verify_source


def tiny_source(tmp_path: Path):
    config = dict(hidden_size=4, intermediate_size=8, num_attention_heads=2,
                  num_key_value_heads=1, head_dim=2, vocab_size=8, num_hidden_layers=1,
                  layernorm_num_groups=2, dtype="bfloat16")
    (tmp_path / "config.json").write_text(json.dumps(config))
    header, offset = {}, 0
    for name, shape in expected_weight_shapes(config).items():
        elements = 1
        for size in shape:
            elements *= size
        header[name] = {"dtype": "F32", "shape": shape, "data_offsets": [offset, offset + elements * 4]}
        offset += elements * 4
    encoded = json.dumps(header).encode()
    encoded += b" " * (-len(encoded) % 8)
    shard = "model-00001-of-00001.safetensors"
    (tmp_path / shard).write_bytes(struct.pack("<Q", len(encoded)) + encoded + b"\0" * offset)
    index = {"metadata": {"total_size": offset}, "weight_map": {name: shard for name in header}}
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps(index))
    return config, index, shard


def test_stored_dtype_wins_over_config_metadata(tmp_path):
    _, _, shard = tiny_source(tmp_path)
    layout = checkpoint_layout(tmp_path)
    assert layout["tensor_count"] == 12
    assert set(layout["elements_by_dtype"]) == {"F32"}
    assert layout["files"][shard]["bytes"] > layout["parameter_elements"] * 4


def test_missing_shard_fails_before_conversion(tmp_path):
    _, _, shard = tiny_source(tmp_path)
    (tmp_path / shard).rename(tmp_path / "partial-download")
    with pytest.raises(FileNotFoundError):
        checkpoint_layout(tmp_path)


@pytest.mark.parametrize("fault", ["truncate", "extra", "mismatched_index", "missing_index", "bad_shape", "escape"])
def test_rejects_invalid_source(tmp_path, fault):
    config, index, shard = tiny_source(tmp_path)
    if fault == "truncate":
        (tmp_path / shard).write_bytes((tmp_path / shard).read_bytes()[:-1])
    elif fault == "extra":
        with (tmp_path / shard).open("ab") as stream:
            stream.write(b"x")
    elif fault == "mismatched_index":
        index["weight_map"]["model.norm.weight"] = "other.safetensors"
    elif fault == "missing_index":
        index["weight_map"]["absent.weight"] = shard
    elif fault == "escape":
        index["weight_map"] = {name: "../" + shard for name in index["weight_map"]}
    elif fault == "bad_shape":
        config["vocab_size"] = 9
        (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps(index))
    with pytest.raises((ValueError, FileNotFoundError)):
        checkpoint_layout(tmp_path)


def test_hash_manifest_requires_explicit_digests(tmp_path):
    path = tmp_path / "hashes.json"
    path.write_text(json.dumps({"sha256_verified_lfs_files": ["weights.safetensors"]}))
    with pytest.raises(ValueError, match="No explicit"):
        read_expected_hashes(path)
    digest = sha256_file(path)
    path.write_text(json.dumps({"files": {"weights.safetensors": digest}}))
    assert read_expected_hashes(path) == {"weights.safetensors": digest}
    path.write_text(json.dumps({"file_details": [{"file": "weights.safetensors", "sha256": digest}]}))
    assert read_expected_hashes(path) == {"weights.safetensors": digest}


def test_real_payload_checksum_must_match_independent_digest(tmp_path):
    _, _, shard = tiny_source(tmp_path)
    for name in ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja", "generation_config.json",
                 "modeling_k2_horizon.py", "offset_fix_baked_norms.json", "k2horCT_provenance.json",
                 "k2horCT_export_verification.json", "publish_manifest.json"):
        (tmp_path / name).write_text("{}")
    hashes = tmp_path / "source-sha256.json"
    hashes.write_text(json.dumps({"files": {shard: sha256_file(tmp_path / shard)}}))
    assert verify_source(tmp_path, hashes)["files"][shard]["sha256"] == sha256_file(tmp_path / shard)
    # Preserve the complete header/layout while changing one actual weight byte.
    with (tmp_path / shard).open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"\x01")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        verify_source(tmp_path, hashes)


def test_four_b_attention_projection_is_not_hidden_size():
    # The 4B checkpoint uses 32×128 query channels, despite hidden_size 2560.
    shapes = expected_weight_shapes(dict(hidden_size=2560, intermediate_size=10240,
        num_attention_heads=32, num_key_value_heads=8, head_dim=128,
        vocab_size=250624, num_hidden_layers=36))
    assert shapes["model.layers.0.self_attn.q_proj.weight"] == (4096, 2560)
    assert shapes["model.layers.0.self_attn.o_proj.weight"] == (2560, 4096)
    assert len(shapes) == 327
