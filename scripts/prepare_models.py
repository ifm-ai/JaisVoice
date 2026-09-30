#!/usr/bin/env python3
"""Reproducibly prepare official IFM models without modifying global caches."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODELS = {
    "k2-cleaner": ("IFM/K2-Horizon-0.9B", "2e1ac41e5676266596f8f2198321d0760f041af6", "K2-Horizon-0.9B-bf16"),
    "k2-agent": ("IFM/K2-Horizon-7B", "ff325e226270e05ea081a97fd0c9c62652472fe8", "K2-Horizon-7B-8bit"),
}


def prepare(runtime: Path, download_only: bool = False, selected: str | None = None):
    os.environ.setdefault("HF_HOME", str(runtime / "cache" / "huggingface"))
    os.environ.setdefault("HF_XET_CACHE", str(runtime / "cache" / "xet"))
    from huggingface_hub import snapshot_download

    runtime.mkdir(parents=True, exist_ok=True)
    manifest_path = runtime / "models" / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for alias, (repo, revision, directory) in MODELS.items():
        if selected and selected != alias:
            continue
        destination = runtime / "models" / directory
        if (destination / "horizon_model.json").exists():
            provenance = json.loads((destination / "horizon_model.json").read_text())
            if provenance["revision"] != revision:
                raise RuntimeError(f"Existing model has a different revision: {destination}")
            manifest[alias] = {"path": str(destination.resolve()), **provenance}
            continue
        print(f"Downloading {repo}@{revision}", flush=True)
        source = runtime / "models" / "sources" / (repo.split("/")[-1] + "-" + revision[:12])
        snapshot_download(
            repo, revision=revision, local_dir=str(source),
            allow_patterns=["*.json", "*.jinja", "*.safetensors", "*.py", "README.md", "LICENSE*"],
            max_workers=4,
        )
        if download_only:
            continue
        print(f"Preparing {alias}", flush=True)
        from k2_runtime.model import convert_checkpoint

        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Refusing to overwrite incomplete/existing model: {destination}. Inspect it first.")
        stage = destination.with_name(destination.name + ".preparing")
        if stage.exists():
            raise RuntimeError(f"Previous preparation exists: {stage}. Inspect it before retrying.")
        stage.mkdir(parents=True)
        convert_checkpoint(source, stage, bits=8 if alias == "k2-agent" else None)
        provenance = {
            "repo": repo, "revision": revision,
            "precision": "8bit-group64" if alias == "k2-agent" else "bfloat16",
            "architecture": "horizon-native-mlx-v1", "prepared_at": time.time(),
        }
        (stage / "horizon_model.json").write_text(json.dumps(provenance, indent=2) + "\n")
        stage.rename(destination)
        manifest[alias] = {"path": str(destination.resolve()), **provenance}
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(manifest_path)
        print(f"Ready: {alias} at {destination}", flush=True)
    if not download_only and manifest:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, default=Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime")))
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--model", choices=list(MODELS))
    args = parser.parse_args()
    prepare(args.runtime_dir, args.download_only, args.model)
