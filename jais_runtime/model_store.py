"""Models: the catalog, their state on this Mac, Hugging Face download and deletion.

`models/catalog.json` lists the speech model and the Jais 3 sizes. A model is
installed when its directory exists under the runtime models directory.
Downloads are pinned to a commit, written to a hidden staging directory
(resumable), checked, then renamed into place.

A language model is the published MLX build, checked against Hugging Face's
sizes and SHA-256. When the repository is not accessible, the files the user
downloaded into `models/sources/<repository name>` are validated and installed
instead.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import fnmatch
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

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "models/catalog.json"
SIZES = ("0.9B", "3.7B", "7B")
# Files of a published MLX build; Python files are never fetched or run.
MODEL_PATTERNS = ["*.json", "*.jinja", "*.safetensors"]
PROVENANCE = "jais_download.json"
Progress = Callable[[dict], None]


def runtime_dir() -> Path:
    return Path(os.environ.get("JAIS_RUNTIME_DIR", ROOT / ".runtime"))


def models_dir(runtime: Path | None = None) -> Path:
    return (runtime or runtime_dir()) / "models"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_catalog(path: Path | None = None) -> list[dict]:
    """Read and check the catalog; any invalid entry rejects the whole file."""
    models = json.loads(Path(path or CATALOG).read_text()).get("models")
    _require(isinstance(models, list), "Invalid model catalog")
    entries, ids, sizes, directories = [], set(), set(), set()
    for entry in models:
        model_id, kind, directory, download = entry.get("id"), entry.get("kind"), entry.get("directory"), entry.get("download")
        _require(isinstance(model_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", model_id) is not None and model_id not in ids,
                 f"Invalid or duplicate identifier: {model_id!r}")
        _require(kind in {"asr", "llm"}, f"Invalid model type for {model_id}")
        _require(isinstance(directory, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", directory) is not None
                 and directory not in directories, f"Invalid or duplicate directory for {model_id}")
        _require(isinstance(entry.get("display_name"), str) and entry["display_name"].strip() != "", f"Missing display name for {model_id}")
        _require(isinstance(download, dict) and re.fullmatch(r"[\w.-]+/[\w.-]+", download.get("repo", "")) is not None,
                 f"Invalid Hugging Face repository for {model_id}")
        # A moving branch would change the weights without changing the catalog.
        _require(re.fullmatch(r"[0-9a-f]{40}", download.get("revision", "")) is not None,
                 f"The revision of {model_id} must be a full commit (40 hexadecimal characters)")
        if kind == "llm":
            _require(entry.get("size") in SIZES and entry["size"] not in sizes, f"Invalid or duplicate size for {model_id}")
            sizes.add(entry["size"])
        else:
            _require(not any(other["kind"] == "asr" for other in entries), "The catalog can contain only one ASR model")
        ids.add(model_id); directories.add(directory)
        entries.append(entry)
    _require(bool(sizes), "The catalog contains no language model")
    return entries


def language_models() -> dict[str, dict]:
    """Catalog language models by size."""
    return {entry["size"]: entry for entry in load_catalog() if entry["kind"] == "llm"}


def find(catalog: list[dict], model_id: str) -> dict:
    for entry in catalog:
        if entry["id"] == model_id:
            return entry
    raise KeyError(f"Unknown model: {model_id}")


def model_path(entry: dict, base: Path) -> Path:
    return base / entry["directory"]


def staging_path(entry: dict, base: Path) -> Path:
    return base / f".{entry['directory']}.download"


def source_path(entry: dict, base: Path) -> Path | None:
    """Where the user can put a language model's files downloaded by hand."""
    return base / "sources" / entry["download"]["repo"].split("/")[-1] if entry["kind"] == "llm" else None


class NoAccess(RuntimeError):
    """The repository is private or gated for this user, and no local copy exists."""

    def __init__(self, repo: str, folder: Path):
        self.repo, self.folder = repo, folder
        super().__init__(f"No access to {repo} on Hugging Face. Request access on https://huggingface.co/{repo} "
                         f"and sign in with `hf auth login`, or download the repository files yourself into {folder}, "
                         "then download the model again.")


def asr_profile(entry: dict) -> str:
    return entry["download"].get("profile", "F5")


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
    partial = sum(disk_bytes(path) for path in (staging_path(entry, base), source_path(entry, base)) if path and path.is_dir())
    return {key: entry[key] for key in ("id", "kind", "display_name", "size") if key in entry} | {
        "installed": installed, "download_bytes": entry["download"].get("bytes"),
        "disk_bytes": disk_bytes(model_path(entry, base)) if installed else 0, "partial_bytes": partial}


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


def validate_model(directory: Path) -> None:
    """Check the complete tensor layout without loading the model or touching Metal."""
    config = json.loads((directory / "config.json").read_text())
    if not isinstance(config, dict) or config.get("model_type") != "k2_horizon":
        raise ValueError(f"Not a Jais 3 configuration: {directory}")
    for key in ("hidden_size", "intermediate_size", "num_attention_heads", "num_key_value_heads", "head_dim", "vocab_size", "num_hidden_layers"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"Invalid dimension ({key}): {directory}")
    groups = config.get("layernorm_num_groups", 1)
    if type(groups) is not int or groups <= 0 or config["hidden_size"] % groups:
        raise ValueError(f"Invalid normalization: {directory}")
    if config.get("num_experts", 0) or config.get("query_key_norm") or config.get("attention_gate_func"):
        raise ValueError(f"Unsupported architecture: {directory}")
    for filename in ("tokenizer.json", "tokenizer_config.json"):
        data = json.loads((directory / filename).read_text())
        if not isinstance(data, dict) or not data:
            raise ValueError(f"Invalid tokenizer metadata: {directory / filename}")
    if not (directory / "chat_template.jinja").read_text().strip():
        raise ValueError(f"Empty template: {directory}")
    index = json.loads((directory / "model.safetensors.index.json").read_text())
    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    if not isinstance(weight_map, dict) or not weight_map or not all(isinstance(shard, str) for shard in weight_map.values()):
        raise ValueError(f"Invalid weight index: {directory}")
    actual = {}
    for filename in sorted(set(weight_map.values())):
        shard = directory / filename
        if Path(filename).name != filename or not filename.endswith(".safetensors") or not shard.is_file():
            raise ValueError(f"Shard missing or outside the model: {filename!r}")
        # safe_open validates the header, offsets and complete payload size.
        with safe_open(shard, framework="np") as weights:
            for name in weights.keys():
                if name in actual or weight_map.get(name) != filename:
                    raise ValueError(f"Duplicate weight or inconsistent index: {name}")
                tensor = weights.get_slice(name)
                actual[name] = (tuple(tensor.get_shape()), tensor.get_dtype())
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
    if set(actual) != set(weight_map) or set(actual) != set(expected):
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
    def report():
        progress({"event": "progress", "phase": "download", "bytes": min(disk_bytes(directory), total), "total": total})
    def watch():
        while not stop.wait(interval):
            report()
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set(); thread.join()
        report()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_python311() -> str:
    bundled = os.environ.get("JAIS_ASR_PYTHON_HOME")
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
    for candidate in (shutil.which("python3.11"), str(Path.home() / ".local/bin/python3.11"),
                      "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.11"):
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Python 3.11 is required for the ASR and was not found (install it, for example with Homebrew).")


def _download_asr(entry: dict, staging: Path, progress: Progress, hub, python: str) -> None:
    spec, profile = entry["download"], asr_profile(entry)
    manifest_name = f"manifests/{profile}.json"
    hub.hf_hub_download(spec["repo"], manifest_name, revision=spec["revision"], local_dir=staging)
    document = json.loads((staging / manifest_name).read_text())
    files = document["files"]
    patterns = sorted(set(document["download_files"]) | {manifest_name, "install.sh", "requirements-mac.txt"})
    with reporting(staging, sum(item["bytes"] for item in files), progress):
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


def _access_error(error: Exception) -> bool:
    """Private, gated or unknown to this account (Hugging Face hides private repositories)."""
    if type(error).__name__ in {"GatedRepoError", "RepositoryNotFoundError", "RevisionNotFoundError"}:
        return True
    return getattr(getattr(error, "response", None), "status_code", None) in (401, 403, 404)


def _source_complete(source: Path) -> bool:
    try:
        index = json.loads((source / "model.safetensors.index.json").read_text())
        needed = {"config.json", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja", *index["weight_map"].values()}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return all((source / name).is_file() for name in needed)


def _download_llm(entry: dict, staging: Path, base: Path, progress: Progress, hub) -> None:
    """Fetch and check the MLX build, or take the copy the user downloaded by hand."""
    spec, source = entry["download"], source_path(entry, base)
    try:
        info = hub.HfApi().model_info(spec["repo"], revision=spec["revision"], files_metadata=True)
    except Exception as error:
        if not _source_complete(source):
            if _access_error(error):
                raise NoAccess(spec["repo"], source) from None
            raise
        # Offline or no access: use the copy the user provided.
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns(".cache"))
        progress({"event": "progress", "phase": "verify"})
        validate_model(staging)
        return
    files = [sibling for sibling in info.siblings if any(fnmatch.fnmatch(sibling.rfilename, pattern) for pattern in MODEL_PATTERNS)]
    with reporting(staging, sum(sibling.size or 0 for sibling in files), progress):
        hub.snapshot_download(spec["repo"], revision=spec["revision"], local_dir=staging, allow_patterns=MODEL_PATTERNS, max_workers=8)
    shutil.rmtree(staging / ".cache", ignore_errors=True)
    progress({"event": "progress", "phase": "verify"})
    for sibling in files:
        path = staging / sibling.rfilename
        if not path.is_file() or (sibling.size is not None and path.stat().st_size != sibling.size):
            raise RuntimeError(f"Incomplete model file: {sibling.rfilename}")
        if sibling.lfs is not None and sha256(path) != sibling.lfs.sha256:
            raise RuntimeError(f"Model file altered: {sibling.rfilename}")
    validate_model(staging)


def download(entry: dict, base: Path, progress: Progress, *, hub=None, python: str | None = None) -> Path:
    """Fetch a model, checked then installed atomically; resumes partial downloads."""
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
            _download_llm(entry, staging, base, progress, hub)
        record = {"id": entry["id"], **entry["download"], "downloaded_at": datetime.now(timezone.utc).isoformat()}
        (staging / PROVENANCE).write_text(json.dumps(record, indent=2) + "\n")
        os.replace(staging, target)
        # The installed model is a checked copy: free the space of the user's files.
        source = source_path(entry, base)
        if source and source.is_dir() and not source.is_symlink():
            shutil.rmtree(source, ignore_errors=True)
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
        source = source_path(entry, base)
        if source and source.is_dir() and not source.is_symlink() and source.resolve().parent == (base / "sources").resolve():
            shutil.rmtree(source)
