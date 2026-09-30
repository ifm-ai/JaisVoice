#!/usr/bin/env python3
"""Build against pinned official binary SDKs, independently of Swift Package Manager.

The default downloads complete archives and verifies the publisher's SHA-256.
--macos-only fetches ZIP ranges for macOS entries from the same HTTPS release,
checking each entry's CRC; this avoids downloading all mobile SDK architectures.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import urllib.request
import zipfile
import zlib

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".framework-downloads"
FRAMEWORKS = ROOT / ".frameworks"
ARTIFACTS = [
    ("LiveKit", "https://github.com/livekit/client-sdk-swift-xcframework/releases/download/2.16.0/LiveKit.xcframework.zip", "1ee04d9739fabb12c0f6688e0a8a805d9753c3868988041ba539a0c4bab2ef25"),
    ("LiveKitWebRTC", "https://github.com/livekit/webrtc-xcframework/releases/download/144.7559.11/LiveKitWebRTC.xcframework.zip", "07c5caf718058af3c528dcabd257298c40e5a8527e4fb9f47c48336ba5899853"),
    ("RustLiveKitUniFFI", "https://github.com/livekit/livekit-uniffi-xcframework/releases/download/0.0.6/RustLiveKitUniFFI.xcframework.zip", "0d3f2ce159a224c728f8b131068d53bbf9b13d968cda0edc68a6a2290f2651ed"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_range(url: str, byte_range: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"Range": f"bytes={byte_range}", "User-Agent": "HorizonVoice-build/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 206:
            raise RuntimeError(f"Server did not support requested ZIP range: HTTP {response.status}")
        return response.read(), response.url


def extract_macos_ranges(name: str, url: str) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as response:
        archive_size = int(response.headers["Content-Length"])
        resolved = response.url
    tail, resolved = request_range(resolved, f"{max(0, archive_size - 65557)}-{archive_size - 1}")
    position = tail.rfind(b"PK\x05\x06")
    if position < 0:
        raise RuntimeError("ZIP central directory footer missing")
    fields = struct.unpack_from("<4s4H2LH", tail, position)
    directory_size, directory_offset = fields[5], fields[6]
    if directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
        raise RuntimeError("ZIP64 archives require full-download mode")
    directory, _ = request_range(resolved, f"{directory_offset}-{directory_offset + directory_size - 1}")
    entries = []
    cursor = 0
    while cursor < len(directory):
        if directory[cursor:cursor + 4] != b"PK\x01\x02":
            raise RuntimeError("Invalid ZIP central directory")
        data = struct.unpack_from("<4s6H3L5H2L", directory, cursor)
        compression, crc, compressed_size = data[4], data[7], data[8]
        name_len, extra_len, comment_len = data[10], data[11], data[12]
        external_attributes, local_offset = data[15], data[16]
        path = directory[cursor + 46:cursor + 46 + name_len].decode("utf-8")
        if "/macos-" in path and not path.startswith("__MACOSX/"):
            entries.append((path, compression, crc, compressed_size, external_attributes, local_offset))
        cursor += 46 + name_len + extra_len + comment_len
    if not entries:
        raise RuntimeError(f"No macOS SDK slice found for {name}")
    print(f"{name}: {len(entries)} macOS entries, {sum(x[3] for x in entries) / 1e6:.1f} MB compressed", flush=True)

    def extract(entry):
        path, compression, crc, compressed_size, attributes, offset = entry
        target = FRAMEWORKS / path
        if not target.resolve().is_relative_to(FRAMEWORKS.resolve()):
            raise RuntimeError("Unsafe archive path")
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        header, _ = request_range(resolved, f"{offset}-{offset + 29}")
        if header[:4] != b"PK\x03\x04":
            raise RuntimeError("Invalid local ZIP header")
        name_length, extra_length = struct.unpack_from("<HH", header, 26)
        start = offset + 30 + name_length + extra_length
        compressed = request_range(resolved, f"{start}-{start + compressed_size - 1}")[0] if compressed_size else b""
        if compression == 0:
            payload = compressed
        elif compression == 8:
            payload = zlib.decompress(compressed, -15)
        else:
            raise RuntimeError(f"Unsupported ZIP compression: {compression}")
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            raise RuntimeError(f"Corrupted ZIP entry: {path}")
        mode = attributes >> 16
        if mode & 0o170000 == 0o120000:
            if not target.is_symlink():
                target.symlink_to(payload.decode())
        else:
            target.write_bytes(payload)
            if mode & 0o777:
                target.chmod(mode & 0o777)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(extract, entries))
    (FRAMEWORKS / f"{name}.source.json").write_text(json.dumps({"url": url, "verification": "HTTPS release URL and per-entry ZIP CRC32", "mode": "macOS slice"}, indent=2))


def prepare(macos_only: bool):
    CACHE.mkdir(parents=True, exist_ok=True)
    FRAMEWORKS.mkdir(parents=True, exist_ok=True)
    directories = []
    for name, url, checksum in ARTIFACTS:
        existing = list(FRAMEWORKS.glob(f"{name}.xcframework/macos-*"))
        if not existing or not (FRAMEWORKS / f"{name}.source.json").exists():
            archive = CACHE / f"{name}.xcframework.zip"
            valid = archive.exists() and sha256(archive) == checksum
            if not valid and macos_only:
                extract_macos_ranges(name, url)
            else:
                if not valid:
                    subprocess.run(["curl", "-fL", "--retry", "3", url, "-o", str(archive)], check=True)
                if sha256(archive) != checksum:
                    raise RuntimeError(f"SHA-256 mismatch: {name}")
                # ditto preserves framework symlinks needed for loading and signing.
                subprocess.run(["ditto", "-x", "-k", str(archive), str(FRAMEWORKS)], check=True)
                (FRAMEWORKS / f"{name}.source.json").write_text(json.dumps({"url": url, "sha256": checksum, "mode": "full archive verified"}, indent=2))
            existing = list(FRAMEWORKS.glob(f"{name}.xcframework/macos-*"))
        if len(existing) != 1:
            raise RuntimeError(f"Expected one macOS slice for {name}, found {existing}")
        directories.append(existing[0])
    return directories


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--macos-only", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--swiftc", default=shutil.which("swiftc"))
    arguments = parser.parse_args()
    frameworks = prepare(arguments.macos_only)
    if arguments.prepare_only:
        print("\n".join(str(path) for path in frameworks)); return
    sdk = arguments.sdk
    if sdk is None:
        compatible = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk")
        sdk = compatible if compatible.exists() else Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
    build = ROOT / ".build" / "direct"
    build.mkdir(parents=True, exist_ok=True)
    command = [arguments.swiftc, "-sdk", str(sdk), "-target", "arm64-apple-macosx14.0", "-swift-version", "5", "-module-cache-path", str(build / "ModuleCache"), "-module-name", "HorizonVoice", "-O", "-o", str(build / "HorizonVoice")]
    for directory in frameworks:
        command += ["-F", str(directory)]
    for name, _, _ in ARTIFACTS:
        command += ["-framework", name]
    command += ["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"]
    command += [str(path) for path in sorted((ROOT / "Sources" / "HorizonVoice").glob("*.swift"))]
    subprocess.run(command, check=True)
    print(json.dumps({"binary": str(build / "HorizonVoice"), "framework_dirs": [str(x) for x in frameworks]}, indent=2))


if __name__ == "__main__":
    main()
