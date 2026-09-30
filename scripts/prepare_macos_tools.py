#!/usr/bin/env python3
"""Build pinned blueutil locally; never install globally or change radio power.

The default operation downloads and compiles source but does NOT execute blueutil.
--check explicitly executes --version only; even that read-only call can trigger
macOS Bluetooth privacy consent because upstream initializes IOBluetooth first.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import tarfile
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / ".runtime/macos-tools"
VERSION = "2.14.0"
SOURCE_ROOT = f"blueutil-{VERSION}"
SOURCE_URL = f"https://github.com/toy/blueutil/archive/refs/tags/v{VERSION}.tar.gz"
SOURCE_SHA256 = "8749883d52be4630c0b557656a487432050cc6e5d125f60d5b1efe646797d152"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024
MAX_MEMBERS = 256
COMPATIBLE_SDK = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk")


def verified_archive(content: bytes) -> bytes:
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("blueutil archive exceeds the download size limit")
    if hashlib.sha256(content).hexdigest() != SOURCE_SHA256:
        raise ValueError("blueutil source checksum mismatch")
    return content


def download_source() -> bytes:
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "HorizonVoice/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("blueutil source must use HTTPS")
        size = response.headers.get("Content-Length")
        if size is not None and int(size) > MAX_ARCHIVE_BYTES:
            raise ValueError("blueutil archive exceeds the download size limit")
        content = response.read(MAX_ARCHIVE_BYTES + 1)
    return verified_archive(content)


def extract_source(content: bytes, destination: Path) -> Path:
    """Validate all entries before writing regular files to an empty directory.

    No tar extract/extractall calls: links, devices, duplicate paths, traversal,
    surprising roots and oversized archives are rejected explicitly.
    """
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("blueutil archive exceeds the download size limit")
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("Extraction destination must be an empty real directory")
    total = 0
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for member in archive:
            if len(members) >= MAX_MEMBERS:
                raise ValueError("Too many entries in blueutil source archive")
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (not name or "\\" in name or "\x00" in name
                    or any(part in ("", ".", "..") for part in parts)
                    or PurePosixPath(name).is_absolute() or parts[0] != SOURCE_ROOT):
                raise ValueError(f"Unsafe source archive path: {member.name!r}")
            if name in names:
                raise ValueError(f"Duplicate source archive path: {name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"Links and special files are forbidden: {name}")
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise ValueError("Source archive entry exceeds the size limit")
            total += member.size
            if total > MAX_EXPANDED_BYTES:
                raise ValueError("Expanded source archive exceeds the size limit")
            names.add(name)
            members.append(member)
        for required in ("blueutil.m", "LICENSE.txt", "Makefile"):
            path = f"{SOURCE_ROOT}/{required}"
            if not any(member.name == path and member.isfile() for member in members):
                raise ValueError(f"Required source file missing: {required}")
        # Check file/directory collisions before any data is written.
        files = {member.name for member in members if member.isfile()}
        for name in names:
            if any(str(parent) in files for parent in PurePosixPath(name).parents):
                raise ValueError(f"Source archive path collides with a file: {name}")
        destination.mkdir(parents=True, exist_ok=True)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Cannot read source archive entry: {member.name}")
            with stream, target.open("xb") as output:
                payload = stream.read(MAX_MEMBER_BYTES + 1)
                if len(payload) != member.size:
                    raise ValueError(f"Truncated source archive entry: {member.name}")
                output.write(payload)
            target.chmod(0o644)
    return destination / SOURCE_ROOT


def resolve_sdk(override: Path | None = None) -> Path:
    """Match the native app's known-compatible SDK before using xcrun defaults."""
    if override is not None:
        candidate = override.expanduser().resolve()
    elif COMPATIBLE_SDK.is_dir():
        candidate = COMPATIBLE_SDK.resolve()
    else:
        value = subprocess.check_output(
            ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
            text=True, timeout=15,
        ).strip()
        if not value or "\n" in value or not Path(value).is_absolute():
            raise ValueError("xcrun returned an invalid macOS SDK path")
        candidate = Path(value).resolve()
    required = ("SDKSettings.plist", "System/Library/Frameworks/Foundation.framework",
                "System/Library/Frameworks/IOBluetooth.framework")
    if not candidate.is_dir() or any(not (candidate / name).exists() for name in required):
        raise ValueError(f"A complete macOS SDK with IOBluetooth is required: {candidate}")
    return candidate


def build_command(source: Path, binary: Path, sdk: Path | None = None) -> list[str]:
    # Audited equivalent of upstream Makefile CFLAGS, with a valid arm64 floor.
    # Do not execute downloaded Makefiles, installers, test scripts or hooks.
    sdk = resolve_sdk(sdk)
    return ["/usr/bin/clang", "-isysroot", str(sdk), "-Wall", "-Wextra", "-Werror", "-arch", "arm64",
            "-mmacosx-version-min=11.0", "-framework", "Foundation", "-framework",
            "IOBluetooth", str(source / "blueutil.m"), "-o", str(binary)]


def installed_manifest(destination: Path = TOOLS_ROOT) -> dict:
    binary = destination / "bin/blueutil"
    manifest_file = destination / "manifest.json"
    if destination.is_symlink() or binary.is_symlink() or manifest_file.is_symlink():
        raise ValueError("Local tool installation must not be a symlink")
    manifest = json.loads(manifest_file.read_text())
    if (not isinstance(manifest, dict) or manifest.get("name") != "blueutil" or manifest.get("version") != VERSION
            or manifest.get("source_url") != SOURCE_URL
            or manifest.get("source_sha256") != SOURCE_SHA256):
        raise ValueError("Local blueutil manifest does not match the pinned release")
    if (not binary.is_file() or not os.access(binary, os.X_OK)
            or hashlib.sha256(binary.read_bytes()).hexdigest() != manifest.get("binary_sha256")):
        raise ValueError("Local blueutil executable is missing or its checksum changed")
    return manifest


def prepare(destination: Path = TOOLS_ROOT, sdk: Path | None = None) -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("This local build supports Apple Silicon macOS only")
    if destination.exists() or destination.is_symlink():
        installed_manifest(destination)
        return destination / "bin/blueutil"
    sdk = resolve_sdk(sdk)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = download_source()
    # Stage the entire installation: failed downloads/builds leave no half-install.
    with tempfile.TemporaryDirectory(prefix=".prepare-macos-tools-", dir=destination.parent) as temp:
        staging = Path(temp) / "installation"
        source = extract_source(content, staging / "sources")
        binary = staging / "bin/blueutil"
        binary.parent.mkdir()
        command = build_command(source, binary, sdk)
        subprocess.run(command, cwd=source, check=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        manifest = {
            "name": "blueutil", "version": VERSION,
            "source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256,
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "architecture": "arm64", "license": "MIT", "macos_sdk": str(sdk),
            "built_at": datetime.now(timezone.utc).isoformat(),
            "build_command": command,
            "note": "Uses private IOBluetooth APIs; no radio state was changed during setup.",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        installed_manifest(staging)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Destination appeared during build; existing files were preserved")
        staging.rename(destination)
    return destination / "bin/blueutil"


def check(destination: Path = TOOLS_ROOT) -> str:
    installed_manifest(destination)
    result = subprocess.run([str(destination / "bin/blueutil"), "--version"],
                            capture_output=True, text=True, check=True, timeout=15)
    version = result.stdout.strip()
    if version not in (VERSION, f"blueutil v{VERSION}", f"v{VERSION}"):
        raise ValueError(f"Unexpected blueutil version output: {version!r}")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Verify existing files and execute --version only (may prompt for Bluetooth privacy).")
    parser.add_argument("--sdk", type=Path, help="Explicit macOS SDK for the local build.")
    args = parser.parse_args(argv)
    try:
        if args.check:
            print(check())
        else:
            print(f"Local blueutil ready: {prepare(sdk=args.sdk)}")
            print("No global installation, Bluetooth access or radio change was performed.")
        return 0
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"Local macOS tools setup failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
