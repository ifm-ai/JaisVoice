#!/usr/bin/env python3
"""Prepare the development environment without changing global Python or Homebrew packages."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    print("Preparing:", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=ROOT, check=True)


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("Run this setup with Python 3.12 or newer.")
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        run(sys.executable, "-m", "venv", ROOT / ".venv")
    lock = ROOT / "requirements.lock"
    run(python, "-m", "pip", "install", "--cache-dir", ROOT / ".runtime/pip-cache", "-r", lock if lock.exists() else ROOT / "requirements.in")
    lock.write_text(subprocess.check_output([str(python), "-m", "pip", "freeze"], text=True))
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        raise SystemExit("Node.js and npm are required for the Pi agent and its browser.")
    run(npm, "ci" if (ROOT / "package-lock.json").exists() else "install", "--ignore-scripts", "--cache", ROOT / ".runtime/npm-cache")
    from agent_tools import install
    install(ROOT / ".runtime/bin", ROOT / ".runtime/build-cache/agent-tools")
    print("Setup complete. Then build the app with scripts/build_app.py.")


if __name__ == "__main__":
    main()
