#!/usr/bin/env python3
"""Build and assemble the native app.

By default the bundle is self-contained, so it can be shared: it embeds Python
3.12 with the service dependencies (the ASR environment is built on it too),
Node.js, and Pi with its extension. The browser is the Mac's Google Chrome, or a
Chromium the agent downloads. User data (models, logs, Pi state, Chromium) lives
in ~/Library/Application Support/Jais Voice, never in the signed bundle.

--dev keeps the previous behaviour: small launchers that run this checkout's
.venv, scripts and .runtime directory, for quick iteration.
"""
import argparse
import datetime
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
APP_SOURCES = ("agent", "jais_runtime", "models", "scripts", "package.json", "package-lock.json")
# The latest release, as published by .github/workflows/release.yml.
UPDATE_FEED = "https://api.github.com/repos/MBZUAI-IFM/JaisVoice/releases/latest"
LAUNCHERS = (("launch-services", "run_services.py"), ("launch-agent", "run_agent.py"), ("launch-models", "models.py"))
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.test.ts")
# What nothing in the app runs: Tk and IDLE, the shared library for embedding
# (the interpreter is static), test suites, and pip (the ASR environment gets its own).
PYTHON_UNUSED = (
    "share", "lib/libpython3.12.dylib", "lib/libtcl*", "lib/tcl9*", "lib/tk9*", "lib/itcl*", "lib/thread*",
    "lib/python3.12/idlelib", "lib/python3.12/tkinter", "lib/python3.12/turtle*", "lib/python3.12/lib2to3",
    "lib/python3.12/pydoc_data", "lib/python3.12/lib-dynload/_tkinter*", "lib/python3.12/site-packages/pip",
    "lib/python3.12/site-packages/pip-*", "lib/python3.12/site-packages/**/tests",
)
# Source maps and type declarations only serve development.
NODE_UNUSED = ("**/*.map", "**/*.d.ts", "**/*.d.mts", "**/*.d.cts")


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def prune(root: Path, patterns) -> None:
    for pattern in patterns:
        # Listed first: a match may sit inside another one removed before it.
        for path in list(root.glob(pattern)):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)


def managed_python(version: str) -> Path:
    """A relocatable python-build-standalone install managed by uv."""
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required to embed Python (brew install uv).")
    run(uv, "python", "install", version)
    executable = Path(subprocess.check_output([uv, "python", "find", "--managed-python", version], text=True).strip())
    return executable.resolve().parents[1]


def portable_node() -> Path:
    node = Path(os.environ.get("JAIS_NODE") or shutil.which("node") or "")
    if not node.is_file():
        raise SystemExit("Node.js is required (official build from nodejs.org).")
    node = node.resolve()
    # A Homebrew node links its own dylibs and would not run on another Mac.
    libraries = subprocess.check_output(["/usr/bin/otool", "-L", str(node)], text=True).splitlines()[1:]
    foreign = [line.strip() for line in libraries if not line.strip().startswith(("/usr/lib/", "/System/"))]
    if foreign:
        raise SystemExit(f"{node} is not a standalone build ({foreign[0]}). Set JAIS_NODE to the official nodejs.org binary.")
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
    prune(home, PYTHON_UNUSED)
    # Package scripts point at the build machine; only the interpreter is run.
    for script in (home / "bin").iterdir():
        if script.name not in ("python", "python3", "python3.12"):
            script.unlink()
    return python


def embed_node(resources: Path, node: Path, identity: str) -> None:
    target = resources / "runtime/node/bin/node"
    target.parent.mkdir(parents=True)
    shutil.copy2(node, target)
    # Debug symbols are a quarter of the binary; stripping voids Node's signature, so sign it again.
    run("/usr/bin/strip", "-x", target)
    run("/usr/bin/codesign", "--force", "--sign", identity, target)


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
    prune(app / "node_modules", NODE_UNUSED)
    return app


def write_launchers(resources: Path, dev: bool) -> None:
    for name, script in LAUNCHERS:
        if dev:
            body = "exec " + shlex.quote(str(ROOT / ".venv/bin/python")) + " " + shlex.quote(str(ROOT / "scripts" / script)) + ' "$@"\n'
        else:
            # Paths are relative to the bundle, which may live anywhere.
            body = f'''HERE="$(cd "$(dirname "$0")" && pwd)"
: "${{JAIS_RUNTIME_DIR:=$HOME/Library/Application Support/Jais Voice}}"
export JAIS_RUNTIME_DIR
export JAIS_NODE="$HERE/runtime/node/bin/node"
export JAIS_ASR_PYTHON_HOME="$HERE/runtime/python"
export JAIS_TOOLS_BIN="$HERE/runtime/bin"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec "$HERE/runtime/python/bin/python3.12" "$HERE/app/scripts/{script}" "$@"
'''
        launcher = resources / name
        launcher.write_text("#!/bin/sh\n" + body)
        launcher.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dev", action="store_true", help="Launchers run this checkout instead of an embedded runtime")
    parser.add_argument("--dmg", action="store_true", help="Also write a drag-to-install disk image")
    parser.add_argument("--build-number", help="CFBundleVersion of this build (CI run number)")
    # macOS ties the Accessibility and Screen Recording permissions to the signature:
    # an ad hoc one changes with every build, so each update asks for them again.
    parser.add_argument("--sign", default=os.environ.get("JAIS_CODESIGN_IDENTITY", "-"),
                        help="Code signing identity (default: ad hoc, or JAIS_CODESIGN_IDENTITY)")
    args = parser.parse_args()
    run(sys.executable, ROOT / "macos/build_direct.py", cwd=ROOT)
    binary = ROOT / "macos/.build/direct/JAISVoice"
    node = None if args.dev else portable_node()
    app = OUTPUTS / "Jais Voice.app"
    # Assemble into a new sibling; keep the previous app as a recoverable backup.
    staging = OUTPUTS / "Jais Voice.building.app"
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
        # CI release builds: numbered, and they look for newer releases.
        info["CFBundleVersion"] = args.build_number
        info["JAISUpdateFeedURL"] = UPDATE_FEED
    with (contents / "Info.plist").open("wb") as destination:
        plistlib.dump(info, destination)
    resources = contents / "Resources"
    resource_dir = ROOT / "macos/Resources"
    if resource_dir.exists():
        shutil.copytree(resource_dir, resources, dirs_exist_ok=True)
    if not args.dev:
        python = embed_python(resources)
        embed_node(resources, node, args.sign)
        bundled_app = embed_app(resources, node)
        from agent_tools import install
        install(resources / "runtime/bin", CACHE / "agent-tools")
        # Bytecode is written now: at run time the signed bundle must stay unchanged.
        run(python, "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash",
            resources / "runtime/python/lib/python3.12", bundled_app)
    # Model services are supervised together; the agent is a pipe child of the app.
    write_launchers(resources, args.dev)
    entitlements = ROOT / "macos/Entitlements.plist"
    signature = ["/usr/bin/codesign", "--force", "--sign", args.sign]
    if entitlements.exists():
        signature += ["--entitlements", str(entitlements)]
    run(*signature, staging)
    run("/usr/bin/codesign", "--verify", "--strict", staging)
    if app.exists():
        backups = ROOT / ".runtime/app-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / ("Jais Voice.previous-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".app")
        app.rename(backup)
        print(f"Previous app preserved: {backup}")
    staging.rename(app)
    print(f"Application ready: {app}")
    if args.dmg:
        from make_dmg import make_dmg
        make_dmg(app, OUTPUTS / f"Jais-Voice-{info['CFBundleShortVersionString']}.dmg", info["CFBundleDisplayName"])


if __name__ == "__main__":
    main()
