"""Installable models: catalog, local state, Hugging Face download and deletion.

`models/catalog.json` (or HORIZON_MODEL_CATALOG) is the only list of model
choices. A model is installed when its directory exists under the runtime
models directory. Downloads are pinned to a commit, written to a hidden
staging directory (resumable), checked, then renamed into place. A model whose
`download` is null is not published yet: it can be used or deleted when
already on disk, never fetched.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Callable

from safetensors import safe_open

from .checkpoint import expected_weight_shapes

ROOT = Path(__file__).resolve().parents[1]
SIZES = ("1B", "4B", "7B")
EFFORTS = ("low", "medium", "high")
CLEANER_CONTEXT = 4096
PROVENANCE = "jais_download.json"
LLM_DEFAULTS = {
    "context_limit": 16384,
    "reasoning_efforts": list(EFFORTS),
    "cleaner_reasoning_effort": "low",
    "agent_reasoning_effort": "low",
    "tool_call_format": "xml",
    "tool_presentation_format": "markdown",
    "loader": "horizon-native-mlx-v1",
    "protocol": "ifm-k2-v1",
}
Progress = Callable[[dict], None]


def runtime_dir() -> Path:
    return Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime"))


def models_dir(runtime: Path | None = None) -> Path:
    return (runtime or runtime_dir()) / "models"


def catalog_path(environ: dict | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("HORIZON_MODEL_CATALOG") or ROOT / "models/catalog.json").expanduser().resolve()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _download_spec(value, kind: str, model_id: str) -> dict | None:
    if value is None:
        return None
    _require(isinstance(value, dict), f"Invalid download for {model_id}")
    repo, revision = value.get("repo"), value.get("revision")
    _require(isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo) is not None, f"Invalid Hugging Face repository for {model_id}")
    # A moving branch would change the weights without changing the catalog.
    _require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
             f"The revision of {model_id} must be a full commit (40 hexadecimal characters)")
    spec = {"repo": repo, "revision": revision}
    if "bytes" in value:
        # Displayed before downloading; the actual size comes from the repository.
        _require(type(value["bytes"]) is int and value["bytes"] > 0, f"Invalid download size for {model_id}")
        spec["bytes"] = value["bytes"]
    if kind == "asr":
        profile = value.get("profile", "F5")
        _require(isinstance(profile, str) and re.fullmatch(r"F\d", profile) is not None, f"Invalid ASR profile for {model_id}")
        spec["profile"] = profile
    return spec


def load_catalog(path: Path | None = None) -> list[dict]:
    """Read and normalize the catalog; any invalid entry rejects the whole file."""
    path = path or catalog_path()
    document = json.loads(Path(path).read_text())
    _require(isinstance(document, dict) and document.get("format") == "jais-voice-models-v1", f"Unknown catalog format: {path}")
    entries, ids, sizes, directories = [], set(), set(), set()
    for raw in document.get("models", []):
        _require(isinstance(raw, dict), "Invalid catalog entry")
        model_id, kind = raw.get("id"), raw.get("kind")
        _require(isinstance(model_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", model_id) is not None and model_id not in ids, f"Invalid or duplicate identifier: {model_id!r}")
        _require(kind in {"asr", "llm"}, f"Invalid model type for {model_id}")
        directory = raw.get("directory")
        _require(isinstance(directory, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", directory) is not None and directory not in directories,
                 f"Invalid or duplicate directory for {model_id}")
        name = raw.get("display_name")
        _require(isinstance(name, str) and name.strip() != "", f"Missing display name for {model_id}")
        entry = {"id": model_id, "kind": kind, "display_name": name.strip(), "directory": directory,
                 "download": _download_spec(raw.get("download"), kind, model_id)}
        if isinstance(raw.get("source"), dict):
            entry["source"] = raw["source"]
        if kind == "llm":
            size = raw.get("size")
            _require(size in SIZES and size not in sizes, f"Invalid or duplicate size for {model_id}")
            entry["size"] = size
            for key, default in LLM_DEFAULTS.items():
                entry[key] = raw.get(key, default)
            _require(type(entry["context_limit"]) is int and 256 <= entry["context_limit"] <= 131072, f"Invalid context_limit for {model_id}")
            efforts = entry["reasoning_efforts"]
            _require(isinstance(efforts, list) and efforts and all(effort in EFFORTS for effort in efforts), f"Invalid reasoning_efforts for {model_id}")
            for role in ("cleaner", "agent"):
                _require(entry[f"{role}_reasoning_effort"] in efforts, f"{role}_reasoning_effort not supported for {model_id}")
            _require(entry["tool_call_format"] in {"xml", "xml_typed", "json"} and entry["tool_presentation_format"] in {"markdown", "xml", "json"},
                     f"Invalid tool format for {model_id}")
            _require(entry["loader"] == "horizon-native-mlx-v1" and entry["protocol"] == "ifm-k2-v1", f"Unsupported loader or protocol for {model_id}")
            sizes.add(size)
        else:
            _require(not any(other["kind"] == "asr" for other in entries), "The catalog can contain only one ASR model")
        ids.add(model_id); directories.add(directory)
        entries.append(entry)
    _require(bool(sizes), "The catalog contains no language model")
    return entries


def find(catalog: list[dict], model_id: str) -> dict:
    for entry in catalog:
        if entry["id"] == model_id:
            return entry
    raise KeyError(f"Unknown model: {model_id}")


def model_path(entry: dict, base: Path) -> Path:
    return base / entry["directory"]


def staging_path(entry: dict, base: Path) -> Path:
    return base / f".{entry['directory']}.download"


def asr_profile(entry: dict) -> str:
    return (entry.get("download") or {}).get("profile", "F5")


def is_installed(entry: dict, base: Path) -> bool:
    path = model_path(entry, base)
    if entry["kind"] == "asr":
        return (path / ".venv/bin/python").exists() and (path / f"manifests/{asr_profile(entry)}.json").is_file()
    return (path / "config.json").is_file()


def disk_bytes(path: Path) -> int:
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(directory, name)).st_size
    return total


def state(entry: dict, base: Path) -> dict:
    installed = is_installed(entry, base)
    staging = staging_path(entry, base)
    result = {key: entry[key] for key in ("id", "kind", "display_name") if key in entry}
    if "size" in entry:
        result["size"] = entry["size"]
    result.update(installed=installed, downloadable=entry["download"] is not None,
                  download_bytes=(entry["download"] or {}).get("bytes"),
                  disk_bytes=disk_bytes(model_path(entry, base)) if installed else 0,
                  partial_bytes=disk_bytes(staging) if staging.is_dir() else 0)
    return result


def validate_prepared_model(directory: Path, entry: dict) -> None:
    """Check complete tensor layout without loading the model or touching Metal.

    This is a structural check, not a content hash or numerical-quality test.
    Conversion provenance and independent inference tests remain necessary.
    """
    config = json.loads((directory / "config.json").read_text())
    if not isinstance(config, dict) or config.get("model_type") != "k2_horizon":
        raise ValueError(f"Incompatible K2 configuration: {directory}")
    for key in ("hidden_size", "intermediate_size", "num_attention_heads", "num_key_value_heads", "head_dim", "vocab_size", "num_hidden_layers"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"Invalid K2 dimension ({key}) : {directory}")
    groups = config.get("layernorm_num_groups", 1)
    if type(groups) is not int or groups <= 0 or config["hidden_size"] % groups:
        raise ValueError(f"Invalid K2 normalization: {directory}")
    if config.get("num_experts", 0) or config.get("query_key_norm") or config.get("attention_gate_func"):
        raise ValueError(f"Unsupported K2 architecture: {directory}")
    maximum = config.get("max_position_embeddings")
    if isinstance(maximum, int) and entry.get("context_limit", 0) > maximum:
        raise ValueError(f"Context limit exceeds the checkpoint: {directory}")
    for filename in ("tokenizer.json", "tokenizer_config.json"):
        data = json.loads((directory / filename).read_text())
        if not isinstance(data, dict) or not data:
            raise ValueError(f"Invalid tokenizer metadata: {directory / filename}")
    if not (directory / "chat_template.jinja").read_text().strip():
        raise ValueError(f"Empty template: {directory}")
    index = json.loads((directory / "model.safetensors.index.json").read_text())
    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    if not isinstance(weight_map, dict) or not weight_map:
        raise ValueError(f"Weight index missing or empty: {directory}")
    if any(not isinstance(name, str) or not name or not isinstance(shard, str) for name, shard in weight_map.items()):
        raise ValueError(f"Invalid weight index: {directory}")
    actual = {}
    for filename in sorted(set(weight_map.values())):
        if Path(filename).name != filename or not filename.endswith(".safetensors"):
            raise ValueError(f"Invalid shard name: {filename!r}")
        shard = directory / filename
        if shard.resolve().parent != directory.resolve() or not shard.is_file():
            raise ValueError(f"Shard missing or outside the model: {shard}")
        # safe_open validates the header, offsets and complete payload size.
        with safe_open(shard, framework="np") as weights:
            for name in weights.keys():
                if name in actual or weight_map.get(name) != filename:
                    raise ValueError(f"Duplicate weight or inconsistent index: {name}")
                tensor = weights.get_slice(name)
                actual[name] = (tuple(tensor.get_shape()), tensor.get_dtype())
    if set(actual) != set(weight_map):
        raise ValueError(f"Indexed weights are missing: {directory}")
    quantization = config.get("quantization")
    if quantization and (quantization.get("bits") != 8 or quantization.get("group_size") != 64 or quantization.get("mode", "affine") != "affine"):
        raise ValueError(f"Unsupported quantization: {directory}")
    expected = {}
    for name, shape in expected_weight_shapes(config).items():
        scales_name = name.removesuffix(".weight") + ".scales"
        if quantization and len(shape) == 2 and scales_name in actual:
            if shape[1] % 64:
                raise ValueError(f"Invalid quantized dimension: {name}")
            expected[name] = ((shape[0], shape[1] // 4), {"U32"})
            expected[scales_name] = ((shape[0], shape[1] // 64), {"BF16", "F16", "F32"})
            expected[name.removesuffix(".weight") + ".biases"] = expected[scales_name]
        else:
            expected[name] = (shape, {"BF16", "F16", "F32"})
    if set(actual) != set(expected):
        raise ValueError(f"Weight names do not match the architecture: {directory}")
    for name, (shape, dtypes) in expected.items():
        if actual[name][0] != shape or actual[name][1] not in dtypes:
            raise ValueError(f"Incompatible weight shape or type: {name}")


@contextlib.contextmanager
def operation_lock(entry: dict, base: Path):
    """One download or deletion per model at a time, across processes."""
    base.mkdir(parents=True, exist_ok=True)
    with open(base / f".{entry['directory']}.lock", "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("An operation is already in progress for this model.") from None
        yield


@contextlib.contextmanager
def reporting(directory: Path, total: int, progress: Progress, interval: float = 0.5):
    """Report bytes present in the staging directory, including partial files."""
    stop = threading.Event()
    def watch():
        while not stop.wait(interval):
            progress({"event": "progress", "phase": "download", "bytes": min(disk_bytes(directory), total), "total": total})
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set(); thread.join()
        progress({"event": "progress", "phase": "download", "bytes": min(disk_bytes(directory), total), "total": total})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_python311() -> str:
    bundled = os.environ.get("HORIZON_ASR_PYTHON_HOME")
    if bundled:
        # The ASR environment points at its interpreter: copy the app's one into
        # the runtime directory so moving or updating the app does not break it.
        home = runtime_dir() / "python3.11"
        if not (home / "bin/python3.11").is_file():
            staging = runtime_dir() / ".python3.11.copy"
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(bundled, staging, symlinks=True)
            shutil.rmtree(home, ignore_errors=True)
            os.replace(staging, home)
        return str(home / "bin/python3.11")
    for candidate in (os.environ.get("HORIZON_ASR_PYTHON"), shutil.which("python3.11"), str(Path.home() / ".local/bin/python3.11"),
                      "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.11"):
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Python 3.11 is required for the ASR and was not found (install it, for example with Homebrew).")


def _download_llm(entry: dict, staging: Path, progress: Progress, hub) -> None:
    spec = entry["download"]
    info = hub.HfApi().model_info(spec["repo"], revision=spec["revision"], files_metadata=True)
    total = sum(sibling.size or 0 for sibling in info.siblings)
    with reporting(staging, total, progress):
        hub.snapshot_download(spec["repo"], revision=spec["revision"], local_dir=staging, max_workers=8)
    progress({"event": "progress", "phase": "verify"})
    shutil.rmtree(staging / ".cache", ignore_errors=True)
    validate_prepared_model(staging, entry)


def _download_asr(entry: dict, staging: Path, progress: Progress, hub, python: str) -> None:
    spec, profile = entry["download"], asr_profile(entry)
    manifest_name = f"manifests/{profile}.json"
    hub.hf_hub_download(spec["repo"], manifest_name, revision=spec["revision"], local_dir=staging)
    document = json.loads((staging / manifest_name).read_text())
    files = document["files"]
    total = sum(item["bytes"] for item in files)
    patterns = sorted(set(document["download_files"]) | {manifest_name, "install.sh", "requirements-mac.txt"})
    with reporting(staging, total, progress):
        hub.snapshot_download(spec["repo"], revision=spec["revision"], local_dir=staging, allow_patterns=patterns, max_workers=8)
    progress({"event": "progress", "phase": "verify"})
    shutil.rmtree(staging / ".cache", ignore_errors=True)
    for item in files:
        path = staging / item["path"]
        if not path.is_file() or path.stat().st_size != item["bytes"] or sha256(path) != item["sha256"]:
            raise RuntimeError(f"Incomplete or altered ASR file: {item['path']}")
    # The published installer builds the model's own Python 3.11 environment.
    progress({"event": "progress", "phase": "install"})
    shutil.rmtree(staging / ".venv", ignore_errors=True)
    subprocess.run(["/bin/bash", str(staging / "install.sh")], cwd=staging, check=True,
                   env={**os.environ, "PYTHON_BIN": python}, stdout=subprocess.DEVNULL, stderr=None)


def download(entry: dict, base: Path, progress: Progress, *, hub=None, python: str | None = None) -> Path:
    """Fetch a published model; resumes a previous partial download."""
    if entry["download"] is None:
        raise RuntimeError(f"{entry['display_name']} is not published on Hugging Face yet.")
    if hub is None:
        import huggingface_hub as hub
    target, staging = model_path(entry, base), staging_path(entry, base)
    with operation_lock(entry, base):
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"{entry['display_name']} is already installed.")
        staging.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "asr":
            _download_asr(entry, staging, progress, hub, python or find_python311())
        else:
            _download_llm(entry, staging, progress, hub)
        record = {"id": entry["id"], **entry["download"], "downloaded_at": datetime.now(timezone.utc).isoformat()}
        (staging / PROVENANCE).write_text(json.dumps(record, indent=2) + "\n")
        os.replace(staging, target)
    progress({"event": "done", "id": entry["id"]})
    return target


def delete(entry: dict, base: Path) -> None:
    """Remove an installed model and any partial download; nothing outside its directory."""
    target, staging = model_path(entry, base), staging_path(entry, base)
    with operation_lock(entry, base):
        if target.is_symlink():
            raise RuntimeError(f"{target} is a symbolic link: remove it manually.")
        for path in (target, staging):
            if path.exists():
                if path.resolve().parent != base.resolve():
                    raise RuntimeError(f"Directory outside the models directory: {path}")
                # Rename first: services see the model disappear at once, never half deleted.
                doomed = base / f".{entry['directory']}.deleting"
                if doomed.exists():
                    shutil.rmtree(doomed)
                os.rename(path, doomed)
                shutil.rmtree(doomed)
