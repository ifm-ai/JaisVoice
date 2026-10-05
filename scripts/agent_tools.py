"""ripgrep and fd, which Pi's grep and find tools run.

Pi would download them on first use, but the agent runs offline: setup.py puts
them in .runtime/bin and build_app.py in the bundle, pinned and checksummed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import tarfile
import urllib.request

# name: (release archive, SHA-256, binary inside the archive). Apple Silicon builds, system libraries only.
TOOLS = {
    "rg": ("https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-aarch64-apple-darwin.tar.gz",
           "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4", "ripgrep-15.2.0-aarch64-apple-darwin/rg"),
    "fd": ("https://github.com/sharkdp/fd/releases/download/v10.5.0/fd-v10.5.0-aarch64-apple-darwin.tar.gz",
           "b67e1836c468e42e411984b56e52fa7abec08c2bd22c867398e7cc134aac5e12", "fd-v10.5.0-aarch64-apple-darwin/fd"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def install(target: Path, cache: Path) -> None:
    """Put rg and fd in `target`, downloading each archive once into `cache`."""
    target.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    for name, (url, expected, member) in TOOLS.items():
        archive = cache / url.rsplit("/", 1)[1]
        if not archive.exists() or sha256(archive) != expected:
            partial = archive.with_suffix(".partial")
            print(f"+ download {url}", flush=True)
            with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            if sha256(partial) != expected:
                partial.unlink()
                raise SystemExit(f"{archive.name} does not match its pinned checksum.")
            partial.replace(archive)
        with tarfile.open(archive) as bundle:
            source = bundle.extractfile(member)
            if source is None:
                raise SystemExit(f"{member} is missing from {archive.name}.")
            destination = target / name
            destination.write_bytes(source.read())
            destination.chmod(0o755)
