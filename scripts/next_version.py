#!/usr/bin/env python3
"""Print the version of the next release: one patch above the latest `v<x.y.z>` tag.

`CFBundleShortVersionString` in macos/Info.plist is the floor. Raise it there to
start a new series: with 1.2.0 the next releases are 1.2.0, 1.2.1, 1.2.2...
"""
import plistlib
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def parse(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Not a major.minor.patch version: {version!r}")
    return tuple(int(part) for part in match.groups())


def next_version(floor: str, tags: list[str]) -> str:
    """The floor until a release reaches it, then one patch above the latest release."""
    released = [parse(match[1]) for tag in tags if (match := re.fullmatch(r"v(\d+\.\d+\.\d+)", tag))]
    latest = max(released, default=None)
    if latest is None or latest < parse(floor):
        return "%d.%d.%d" % parse(floor)
    return f"{latest[0]}.{latest[1]}.{latest[2] + 1}"


def remote_tags(remote: str = "origin") -> list[str]:
    output = subprocess.check_output(["git", "ls-remote", "--tags", "--refs", remote], cwd=ROOT, text=True)
    return [line.split("refs/tags/", 1)[1] for line in output.splitlines()]


def main():
    with (ROOT / "macos/Info.plist").open("rb") as source:
        floor = plistlib.load(source)["CFBundleShortVersionString"]
    print(next_version(floor, remote_tags()))


if __name__ == "__main__":
    main()
