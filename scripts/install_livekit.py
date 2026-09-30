#!/usr/bin/env python3
"""Install the verified Homebrew macOS ARM64 bottle locally, without Homebrew writes."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime"))
VERSION = "1.13.7"
SHA = "0302c5e72f2a2a56f0194efd1949e416ea61807701635d54fe3e80b09f7744e1"


def main():
    destination = RUNTIME / "bin" / "livekit-server"
    if destination.exists():
        print(f"LiveKit already installed: {destination}")
        return
    token_url = "https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/livekit:pull"
    with urllib.request.urlopen(token_url, timeout=30) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"https://ghcr.io/v2/homebrew/core/livekit/blobs/sha256:{SHA}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "HorizonVoice/0.1"},
    )
    print(f"Downloading LiveKit {VERSION} for Apple Silicon…", flush=True)
    with urllib.request.urlopen(request, timeout=120) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != SHA:
        raise RuntimeError("LiveKit bottle checksum mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        candidates = [m for m in archive.getmembers() if m.isfile() and m.name.endswith("/bin/livekit-server")]
        if len(candidates) != 1:
            raise RuntimeError("LiveKit executable missing from verified bottle")
        with archive.extractfile(candidates[0]) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
    destination.chmod(0o755)
    (RUNTIME / "livekit-version.json").write_text(json.dumps({"version": VERSION, "bottle_sha256": SHA}, indent=2))
    print(f"Installed {destination}")


if __name__ == "__main__":
    main()
