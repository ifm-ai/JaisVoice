#!/usr/bin/env python3
"""Verify local mblab exports and convert them; never download or execute source code.

Writes manifest-mblab.json only. Activating this profile is a separate operation.
The norm-offset fix is already present in the HF weights and must not be repeated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from k2_runtime.checkpoint import sha256_file, verify_source

MODELS = {
    "k2-cleaner": {
        "repo": "MBZUAI-Paris/A13x1-1Bcv5c189-sft-reasoningv9.5.2k2horCT-ckpt10711-FTPO_es035",
        "revision": "32bfb4a00c1e56a9956fd279b2743e1336ea7fe6",
        "source_directory": "mblab-1B-32bfb4a00c1e", "directory": "MBZUAI-1B-32bfb4a00c1e-bf16",
        "display_name": "MBZUAI · 1B", "context_limit": 4096, "default_reasoning_effort": "low",
        "hidden_size": 1536, "num_hidden_layers": 28, "layernorm_num_groups": 1,
        "vocab_size": 64256, "rope_theta": 1_000_000.0, "bits": None,
    },
    "k2-agent": {
        "repo": "MBZUAI-Paris/A13cs_A13x1-4Bc10248_sft_reasoningv9.5.1k2horCT_FTPO_es035",
        "revision": "20e95a2e3f4452c6d26374d48cbc9902259d49bf",
        "source_directory": "mblab-4B-20e95a2e3f44", "directory": "MBZUAI-4B-20e95a2e3f44-8bit",
        "display_name": "MBZUAI · 4B", "context_limit": 16384, "default_reasoning_effort": "low",
        "hidden_size": 2560, "num_hidden_layers": 36, "layernorm_num_groups": 2,
        "vocab_size": 250624, "rope_theta": 10_000_000.0, "bits": 8,
    },
}
REFERENCE_MODEL_SHA256 = "fb09e010956bd51cfa7d4055b4381cff34c9e06164066b49e3546f38b2e6242f"


def verify_pinned_source(source: Path, spec: dict, hashes_path: Path) -> dict:
    provenance = json.loads((source / "download-manifest.json").read_text())
    for key in ("repo", "revision"):
        if provenance.get(key) != spec[key]:
            raise ValueError(f"Pinned source {key} differs: {source}")
    config = json.loads((source / "config.json").read_text())
    for key in ("hidden_size", "num_hidden_layers", "layernorm_num_groups", "vocab_size"):
        if config.get(key) != spec[key]:
            raise ValueError(f"Pinned architecture {key} differs: {source}")
    rope = config.get("rope_parameters", {})
    if rope.get("rope_type") != "default" or rope.get("rope_theta") != spec["rope_theta"]:
        raise ValueError(f"Pinned RoPE configuration differs: {source}")
    if sha256_file(source / "modeling_k2_horizon.py") != REFERENCE_MODEL_SHA256:
        raise ValueError("The reference architecture changed; review its code before converting")
    norm_fix = json.loads((source / "offset_fix_baked_norms.json").read_text())
    if norm_fix.get("num_norm_tensors") != spec["num_hidden_layers"] * 2 + 1 or "baked HF-style norm weights" not in norm_fix.get("action", ""):
        raise ValueError("Expected already-baked norm-offset provenance is missing")
    verified = verify_source(source, hashes_path)
    if set(verified["elements_by_dtype"]) != {"F32"}:
        raise ValueError("Pinned source was expected to contain physical FP32 tensors")
    return verified


def write_json(path: Path, document: dict):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def prepare(runtime: Path, selected: str | None = None, verify_only: bool = False,
            hashes_1b: Path | None = None, hashes_4b: Path | None = None) -> dict:
    runtime = runtime.resolve()
    manifest_path = runtime / "models" / "manifest-mblab.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    reports = {}
    for alias, spec in MODELS.items():
        if selected and alias != selected:
            continue
        source = runtime / "models" / "sources" / spec["source_directory"]
        hashes_path = (hashes_1b or source / "source-sha256.json") if alias == "k2-cleaner" else (hashes_4b or source / "download-manifest.json")
        print(f"Verifying complete local source: {alias}", flush=True)
        verified = verify_pinned_source(source, spec, hashes_path)
        reports[alias] = verified
        if verify_only:
            print(f"Verified {verified['tensor_count']} FP32 tensors ({verified['parameter_elements']:,} elements)", flush=True)
            continue
        destination = runtime / "models" / spec["directory"]
        provenance_path = destination / "horizon_model.json"
        if destination.exists():
            if not provenance_path.exists():
                raise RuntimeError(f"Refusing to overwrite existing incomplete target: {destination}")
            provenance = json.loads(provenance_path.read_text())
            if provenance.get("revision") != spec["revision"] or provenance.get("source_verification") != verified:
                raise RuntimeError(f"Existing conversion provenance differs: {destination}")
            for filename, digest in provenance["output_sha256"].items():
                if sha256_file(destination / filename) != digest:
                    raise ValueError(f"Existing converted file differs: {filename}")
        else:
            stage = destination.with_name(destination.name + ".preparing")
            if stage.exists():
                raise RuntimeError(f"Previous preparation exists; inspect before retrying: {stage}")
            stage.mkdir(parents=True)
            from k2_runtime.model import convert_checkpoint
            convert_checkpoint(source, stage, bits=spec["bits"])
            # Keep audit metadata but never copy or execute remote Python modules.
            for filename in ("offset_fix_baked_norms.json", "k2horCT_provenance.json", "k2horCT_export_verification.json", "publish_manifest.json"):
                shutil.copy2(source / filename, stage / filename)
            output_hashes = {path.name: sha256_file(path) for path in sorted(stage.iterdir()) if path.is_file()}
            provenance = {
                "repo": spec["repo"], "revision": spec["revision"],
                "display_name": spec["display_name"], "context_limit": spec["context_limit"],
                "precision": "8bit-group64" if spec["bits"] else "bfloat16",
                "architecture": "horizon-native-mlx-v1", "loader": "horizon-native-mlx-v1",
                "protocol": "ifm-k2-v1", "reasoning_efforts": ["low", "medium", "high"],
                "tool_call_format": "xml", "tool_presentation_format": "markdown",
                "prepared_at": datetime.now(timezone.utc).isoformat(),
                "source_host": "mblab", "source_path": str(source),
                "source_verification": verified, "output_sha256": output_hashes,
                "norm_offset": "already baked in source; no offset added during MLX conversion",
            }
            write_json(stage / "horizon_model.json", provenance)
            stage.rename(destination)
        # A launch preference belongs to the profile, not immutable weight provenance.
        manifest[alias] = {"path": str(destination), **provenance,
                           "default_reasoning_effort": spec["default_reasoning_effort"]}
        write_json(manifest_path, manifest)
        print(f"Ready: {alias} → {destination}", flush=True)
    if not verify_only:
        print(f"Profile written, not activated: {manifest_path}", flush=True)
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime")))
    parser.add_argument("--model", choices=list(MODELS))
    parser.add_argument("--verify-only", action="store_true", help="Validate hashes, index, tensor shapes and source provenance without loading MLX")
    parser.add_argument("--hashes-1b", type=Path)
    parser.add_argument("--hashes-4b", type=Path)
    args = parser.parse_args()
    prepare(args.runtime_dir, args.model, args.verify_only, args.hashes_1b, args.hashes_4b)
