#!/usr/bin/env python3
"""Build and assemble the native app.

By default the bundle is self-contained, so it can be shared: it embeds Python
3.12 with the service dependencies, Python 3.11 for the ASR environment, Node.js,
Pi with its extension, and Chromium. User data (models, logs, Pi state) lives in
~/Library/Application Support/JAIS Voice, never in the signed bundle.

--dev keeps the previous behaviour: small launchers that run this checkout's
.venv, scripts and .runtime directory, for quick iteration.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
CACHE = ROOT / ".runtime/build-cache"
# Project files needed at run time, copied into Contents/Resources/app.
APP_SOURCES = ("agent", "backend/horizon_voice", "k2_runtime", "models", "scripts", "package.json", "package-lock.json")
LAUNCHERS = (("launch-services", "run_services.py"), ("launch-agent", "run_agent.py"), ("launch-models", "models.py"))
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.test.ts")


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def managed_python(version: str) -> Path:
    """A relocatable python-build-standalone install managed by uv."""
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required to embed Python (brew install uv).")
    run(uv, "python", "install", version)
    executable = Path(subprocess.check_output([uv, "python", "find", "--managed-python", version], text=True).strip())
    return executable.resolve().parents[1]


def portable_node() -> Path:
    node = Path(os.environ.get("HORIZON_NODE") or shutil.which("node") or "")
    if not node.is_file():
        raise SystemExit("Node.js is required (official build from nodejs.org).")
    node = node.resolve()
    # A Homebrew node links its own dylibs and would not run on another Mac.
    libraries = subprocess.check_output(["/usr/bin/otool", "-L", str(node)], text=True).splitlines()[1:]
    foreign = [line.strip() for line in libraries if not line.strip().startswith(("/usr/lib/", "/System/"))]
    if foreign:
        raise SystemExit(f"{node} is not a standalone build ({foreign[0]}). Set HORIZON_NODE to the official nodejs.org binary.")
    return node


def embed_python(resources: Path) -> Path:
    home = resources / "runtime/python"
    shutil.copytree(managed_python("3.12"), home, symlinks=True, ignore=IGNORED)
    python = home / "bin/python3.12"
    # uv marks its interpreters as externally managed; this copy belongs to the app.
    for marker in home.glob("lib/python3.12/EXTERNALLY-MANAGED"):
        marker.unlink()
    requirements = CACHE / "requirements-app.txt"
    CACHE.mkdir(parents=True, exist_ok=True)
    lines = (ROOT / "requirements.in").read_text().splitlines()
    requirements.write_text("\n".join(line for line in lines if not line.startswith("pytest")) + "\n")
    run("uv", "pip", "install", "--python", python, "--cache-dir", CACHE / "uv", "-r", requirements, "-c", ROOT / "requirements.lock")
    return python


def embed_python311(resources: Path) -> None:
    shutil.copytree(managed_python("3.11"), resources / "runtime/python3.11", symlinks=True, ignore=IGNORED)


def embed_app(resources: Path, node: Path) -> Path:
    app = resources / "app"
    for name in APP_SOURCES:
        source, target = ROOT / name, app / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=IGNORED)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required to install Pi into the bundle.")
    env = dict(os.environ, PATH=os.pathsep.join([str(node.parent), os.environ.get("PATH", "")]))
    run(npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", "--cache", CACHE / "npm", cwd=app, env=env)
    # esbuild ships one optional binary package per platform; only this one can run.
    for platform in app.glob("node_modules/**/@esbuild/*"):
        if platform.is_dir() and platform.name != "darwin-arm64":
            shutil.rmtree(platform)
    return app


def embed_chromium(resources: Path, app: Path, node: Path) -> None:
    """Copy the Chromium revision this Playwright expects; the cache avoids re-downloading it."""
    browsers = CACHE / "browsers"
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(browsers))
    run(node, app / "node_modules/playwright/cli.js", "install", "chromium", "--no-shell", env=env)
    manifest = json.loads((app / "node_modules/playwright-core/browsers.json").read_text())
    revision = next(browser["revision"] for browser in manifest["browsers"] if browser["name"] == "chromium")
    shutil.copytree(browsers / f"chromium-{revision}", resources / f"runtime/browsers/chromium-{revision}", symlinks=True)


def write_launchers(resources: Path, dev: bool) -> None:
    for name, script in LAUNCHERS:
        if dev:
            body = "exec " + shlex.quote(str(ROOT / ".venv/bin/python")) + " " + shlex.quote(str(ROOT / "scripts" / script)) + ' "$@"\n'
        else:
            # Paths are relative to the bundle, which may live anywhere.
            body = f'''HERE="$(cd "$(dirname "$0")" && pwd)"
: "${{HORIZON_RUNTIME_DIR:=$HOME/Library/Application Support/JAIS Voice}}"
export HORIZON_RUNTIME_DIR
export HORIZON_NODE="$HERE/runtime/node/bin/node"
export HORIZON_ASR_PYTHON_HOME="$HERE/runtime/python3.11"
export PLAYWRIGHT_BROWSERS_PATH="$HERE/runtime/browsers"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec "$HERE/runtime/python/bin/python3.12" "$HERE/app/scripts/{script}" "$@"
'''
        launcher = resources / name
        launcher.write_text("#!/bin/sh\n" + body)
        launcher.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, help="Use a prebuilt native binary")
    parser.add_argument("--direct", action="store_true", help="Build with swiftc, without SwiftPM")
    parser.add_argument("--swift", default=os.environ.get("HORIZON_SWIFT", "swift"))
    parser.add_argument("--dev", action="store_true", help="Launchers run this checkout instead of an embedded runtime")
    parser.add_argument("--zip", action="store_true", help="Also write a zip archive to share the app")
    parser.add_argument("--dmg", action="store_true", help="Also write a drag-to-install disk image")
    parser.add_argument("--build-number", help="CFBundleVersion of this build (CI run number)")
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
    node = None if args.dev else portable_node()
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
    if args.build_number:
        info["CFBundleVersion"] = args.build_number
    with (contents / "Info.plist").open("wb") as destination:
        plistlib.dump(info, destination)
    resources = contents / "Resources"
    resource_dir = ROOT / "macos/Resources"
    if resource_dir.exists():
        shutil.copytree(resource_dir, resources, dirs_exist_ok=True)
    if not args.dev:
        python = embed_python(resources)
        embed_python311(resources)
        (resources / "runtime/node/bin").mkdir(parents=True)
        shutil.copy2(node, resources / "runtime/node/bin/node")
        bundled_app = embed_app(resources, node)
        embed_chromium(resources, bundled_app, node)
        # Bytecode is written now: at run time the signed bundle must stay unchanged.
        run(python, "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash",
            resources / "runtime/python/lib/python3.12", bundled_app)
    # Model services are supervised together; the agent is a pipe child of the app.
    write_launchers(resources, args.dev)
    entitlements = ROOT / "macos/Entitlements.plist"
    signature = ["/usr/bin/codesign", "--force", "--sign", "-"]
    if entitlements.exists():
        signature += ["--entitlements", str(entitlements)]
    run(*signature, staging)
    run("/usr/bin/codesign", "--verify", "--strict", staging)
    if app.exists():
        backups = ROOT / ".runtime/app-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / ("JAIS Voice.previous-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".app")
        app.rename(backup)
        print(f"Previous app preserved: {backup}")
    staging.rename(app)
    print(f"Application ready: {app}")
    if args.zip:
        archive = OUTPUTS / f"JAIS-Voice-{info['CFBundleShortVersionString']}.zip"
        archive.unlink(missing_ok=True)
        run("/usr/bin/ditto", "-c", "-k", "--keepParent", app, archive)
        print(f"Archive ready: {archive}")
    if args.dmg:
        from make_dmg import make_dmg
        make_dmg(app, OUTPUTS / f"JAIS-Voice-{info['CFBundleShortVersionString']}.dmg", info["CFBundleDisplayName"])


if __name__ == "__main__":
    main()
