#!/usr/bin/env python3
"""Select a validated local model pair without overwriting the IFM manifest."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from safetensors import SafetensorError, safe_open
from k2_runtime.checkpoint import expected_weight_shapes
from k2_runtime.gateway import MODEL_ALIASES, read_model_manifest


def selection_path(runtime: Path) -> Path:
    return runtime / "models" / "active-profile.json"


def validate_prepared_model(directory: Path, entry: dict) -> None:
    """Check complete tensor layout without loading the model or touching Metal.

    This is a structural check, not a content hash or numerical-quality test.
    Conversion provenance and independent inference tests remain necessary.
    """
    config = json.loads((directory / "config.json").read_text())
    if not isinstance(config, dict) or config.get("model_type") != "k2_horizon":
        raise ValueError(f"Configuration K2 incompatible : {directory}")
    for key in ("hidden_size", "intermediate_size", "num_attention_heads", "num_key_value_heads", "head_dim", "vocab_size", "num_hidden_layers"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"Dimension K2 invalide ({key}) : {directory}")
    groups = config.get("layernorm_num_groups", 1)
    if type(groups) is not int or groups <= 0 or config["hidden_size"] % groups:
        raise ValueError(f"Normalisation K2 invalide : {directory}")
    if config.get("num_experts", 0) or config.get("query_key_norm") or config.get("attention_gate_func"):
        raise ValueError(f"Architecture K2 non prise en charge : {directory}")
    maximum = config.get("max_position_embeddings")
    if isinstance(maximum, int) and entry["context_limit"] > maximum:
        raise ValueError(f"Limite de contexte supérieure au checkpoint : {directory}")
    for filename in ("tokenizer.json", "tokenizer_config.json"):
        data = json.loads((directory / filename).read_text())
        if not isinstance(data, dict) or not data:
            raise ValueError(f"Métadonnées tokenizer invalides : {directory / filename}")
    if not (directory / "chat_template.jinja").read_text().strip():
        raise ValueError(f"Template vide : {directory}")
    index = json.loads((directory / "model.safetensors.index.json").read_text())
    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    if not isinstance(weight_map, dict) or not weight_map:
        raise ValueError(f"Index des poids absent ou vide : {directory}")
    if any(not isinstance(name, str) or not name or not isinstance(shard, str) for name, shard in weight_map.items()):
        raise ValueError(f"Index des poids invalide : {directory}")
    actual = {}
    for filename in sorted(set(weight_map.values())):
        if Path(filename).name != filename or not filename.endswith(".safetensors"):
            raise ValueError(f"Nom de shard invalide : {filename!r}")
        shard = directory / filename
        if shard.resolve().parent != directory.resolve() or not shard.is_file():
            raise ValueError(f"Shard absent ou hors du modèle : {shard}")
        # safe_open validates the header, offsets and complete payload size.
        with safe_open(shard, framework="np") as weights:
            for name in weights.keys():
                if name in actual or weight_map.get(name) != filename:
                    raise ValueError(f"Poids dupliqué ou index incohérent : {name}")
                tensor = weights.get_slice(name)
                actual[name] = (tuple(tensor.get_shape()), tensor.get_dtype())
    if set(actual) != set(weight_map):
        raise ValueError(f"Des poids indexés sont absents : {directory}")
    quantization = config.get("quantization")
    if quantization and (quantization.get("bits") != 8 or quantization.get("group_size") != 64 or quantization.get("mode", "affine") != "affine"):
        raise ValueError(f"Quantification non prise en charge : {directory}")
    expected = {}
    for name, shape in expected_weight_shapes(config).items():
        scales_name = name.removesuffix(".weight") + ".scales"
        if quantization and len(shape) == 2 and scales_name in actual:
            if shape[1] % 64:
                raise ValueError(f"Dimension quantifiée invalide : {name}")
            expected[name] = ((shape[0], shape[1] // 4), {"U32"})
            expected[scales_name] = ((shape[0], shape[1] // 64), {"BF16", "F16", "F32"})
            expected[name.removesuffix(".weight") + ".biases"] = expected[scales_name]
        else:
            expected[name] = (shape, {"BF16", "F16", "F32"})
    if set(actual) != set(expected):
        raise ValueError(f"Les noms des poids ne correspondent pas à l’architecture : {directory}")
    for name, (shape, dtypes) in expected.items():
        if actual[name][0] != shape or actual[name][1] not in dtypes:
            raise ValueError(f"Forme ou type de poids incompatible : {name}")


def validate_profile(manifest: Path) -> dict:
    try:
        entries = read_model_manifest(manifest)
        for alias in MODEL_ALIASES:
            validate_prepared_model(Path(entries[alias]["path"]), entries[alias])
        return entries
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SafetensorError) as error:
        raise ValueError(f"Profil de modèles invalide ({manifest}) : {error}") from error


def resolve_model_profile(runtime: Path, environ: dict | None = None) -> Path:
    """Environment override > persistent selector > unchanged IFM default."""
    environ = os.environ if environ is None else environ
    if "HORIZON_MODEL_MANIFEST" in environ:
        explicit = environ["HORIZON_MODEL_MANIFEST"]
        if not isinstance(explicit, str) or not explicit.strip():
            raise ValueError("HORIZON_MODEL_MANIFEST est défini mais vide.")
        manifest = Path(explicit).expanduser().resolve()
    elif selection_path(runtime).exists():
        selector = selection_path(runtime)
        try:
            selected = json.loads(selector.read_text())
            if not isinstance(selected, dict) or type(selected.get("version")) is not int or selected["version"] != 1 or not isinstance(selected.get("manifest"), str) or not selected["manifest"].strip():
                raise ValueError("format attendu : version 1 et chemin manifest")
            manifest = Path(selected["manifest"]).expanduser()
            if not manifest.is_absolute():
                manifest = selector.parent / manifest
            manifest = manifest.resolve()
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"Sélection de modèles illisible ({selector}) : {error}") from error
    else:
        manifest = (runtime / "models" / "manifest.json").resolve()
    validate_profile(manifest)
    return manifest


def select_profile(runtime: Path, manifest: Path) -> Path:
    manifest = manifest.expanduser().resolve()
    validate_profile(manifest)
    selector = selection_path(runtime)
    selector.parent.mkdir(parents=True, exist_ok=True)
    relative = os.path.relpath(manifest, selector.parent)
    document = {"version": 1, "manifest": relative, "selected_at": datetime.now(timezone.utc).isoformat()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=selector.parent, prefix=".active-profile-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selector)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return manifest


def reset_profile(runtime: Path) -> None:
    # This exact selector is the only removed file; all manifests/weights remain.
    selection_path(runtime).unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime")))
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--manifest", type=Path)
    choice.add_argument("--reset", action="store_true", help="Remove only the persisted selection; keep every model and manifest")
    args = parser.parse_args(argv)
    try:
        if args.reset:
            reset_profile(args.runtime_dir)
            print("Sélection réinitialisée : manifest.json IFM sera utilisé au prochain lancement. Aucun modèle supprimé.")
        else:
            selected = select_profile(args.runtime_dir, args.manifest)
            print(f"Profil sélectionné pour le prochain lancement : {selected}")
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
