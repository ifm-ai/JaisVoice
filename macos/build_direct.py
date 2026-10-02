#!/usr/bin/env python3
"""Compile the native app with swiftc, independently of Swift Package Manager.

The app has no third-party binary framework: it talks to the local services
over HTTP and to the Pi agent over a pipe.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--swiftc", default=shutil.which("swiftc"))
    arguments = parser.parse_args()
    sdk = arguments.sdk
    if sdk is None:
        # The installed SDK 27.0 is newer than the compiler; 26.5 matches it.
        compatible = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk")
        sdk = compatible if compatible.exists() else Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
    build = ROOT / ".build" / "direct"
    build.mkdir(parents=True, exist_ok=True)
    command = [arguments.swiftc, "-sdk", str(sdk), "-target", "arm64-apple-macosx26.0", "-swift-version", "5",
               "-module-cache-path", str(build / "ModuleCache"), "-module-name", "JAISVoice", "-O", "-o", str(build / "JAISVoice")]
    command += [str(path) for path in sorted((ROOT / "Sources" / "JAISVoice").glob("*.swift"))]
    subprocess.run(command, check=True)
    print(json.dumps({"binary": str(build / "JAISVoice")}, indent=2))


if __name__ == "__main__":
    main()
