#!/usr/bin/env python3
"""Build and assemble the native app, with only project-owned runtime children."""
import argparse
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"


def run(*args, **kwargs):
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, help="Use a prebuilt native binary")
    parser.add_argument("--direct", action="store_true", help="Build with swiftc, without SwiftPM")
    parser.add_argument("--swift", default=os.environ.get("HORIZON_SWIFT", "swift"))
    args = parser.parse_args()
    if args.direct:
        run(sys.executable, ROOT / "macos/build_direct.py", cwd=ROOT)
        binary = ROOT / "macos/.build/direct/HorizonVoice"
    elif args.binary:
        binary = args.binary.resolve()
    else:
        run(args.swift, "build", "-c", "release", "--disable-sandbox", cwd=ROOT / "macos")
        location = subprocess.check_output([args.swift, "build", "-c", "release", "--show-bin-path"], cwd=ROOT / "macos", text=True).strip()
        binary = Path(location) / "HorizonVoice"
    if not binary.is_file():
        raise SystemExit(f"Native binary missing: {binary}")
    app = OUTPUTS / "JAIS Voice.app"
    # Assemble into a new sibling; keep the previous app as a recoverable backup.
    staging = OUTPUTS / "JAIS Voice.building.app"
    if staging.exists():
        raise SystemExit(f"A prior build remains at {staging}; inspect it before replacing.")
    contents = staging / "Contents"
    for name in ("MacOS", "Resources"):
        (contents / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, contents / "MacOS/JAISVoice")
    with (ROOT / "macos/Info.plist").open("rb") as source:
        info = plistlib.load(source)
    info["CFBundleExecutable"] = "JAISVoice"
    with (contents / "Info.plist").open("wb") as destination:
        plistlib.dump(info, destination)
    resource_dir = ROOT / "macos/Resources"
    if resource_dir.exists():
        shutil.copytree(resource_dir, contents / "Resources", dirs_exist_ok=True)
    # Model services are supervised together; the agent is a pipe child of the app.
    for name, script in (("launch-services", "run_services.py"), ("launch-agent", "run_agent.py")):
        launcher = contents / "Resources" / name
        launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(ROOT / ".venv/bin/python")) + " " + shlex.quote(str(ROOT / "scripts" / script)) + "\n")
        launcher.chmod(0o755)
    entitlements = ROOT / "macos/Entitlements.plist"
    signature = ["/usr/bin/codesign", "--force", "--sign", "-"]
    if entitlements.exists():
        signature += ["--entitlements", str(entitlements)]
    run(*signature, staging)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", staging)
    if app.exists():
        import datetime
        backups = ROOT / ".runtime/app-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / ("JAIS Voice.previous-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".app")
        app.rename(backup)
        print(f"Previous app preserved: {backup}")
    staging.rename(app)
    print(f"Application ready: {app}")


if __name__ == "__main__":
    main()
