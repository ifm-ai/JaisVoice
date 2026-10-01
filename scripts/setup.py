#!/usr/bin/env python3
"""Prepare this local demo without changing global Python or Homebrew packages."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    print("Preparing:", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=ROOT, env=env, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args()
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
        raise SystemExit("Node.js and npm are required for the Pi agent and the dedicated browser.")
    run(npm, "ci" if (ROOT / "package-lock.json").exists() else "install", "--ignore-scripts", "--cache", ROOT / ".runtime/npm-cache")
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".runtime/browsers")
    run(node, ROOT / "node_modules/playwright/cli.js", "install", "chromium", "--no-shell", env=env)
    if not args.skip_models:
        run(python, ROOT / "scripts/prepare_models.py")
    print("Préparation terminée. Construire ensuite l’application avec scripts/build_app.py.")


if __name__ == "__main__":
    main()
