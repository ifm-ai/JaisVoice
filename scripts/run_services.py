#!/usr/bin/env python3
"""Own and supervise the model services (ASR, MLX gateway) for one JAIS Voice instance.

The Pi agent is not supervised here: the native app runs it as a pipe child.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.select_model_profile import resolve_model_profile
from k2_runtime.gateway import read_model_manifest

RUNTIME = Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime"))
STOP = threading.Event()


def shutdown(signum=None, frame=None):
    STOP.set()


def apply_profile_reasoning(env: dict, entries: dict) -> None:
    for alias, variable in (("k2-cleaner", "HORIZON_CLEANER_REASONING_EFFORT"),
                            ("k2-agent", "HORIZON_AGENT_REASONING_EFFORT")):
        env.setdefault(variable, entries[alias]["default_reasoning_effort"])
        if env[variable] not in entries[alias]["reasoning_efforts"]:
            raise ValueError(f"{variable}={env[variable]!r} n’est pas pris en charge par le profil sélectionné.")


def acquire_lock(lock, timeout=15):
    deadline = time.monotonic() + timeout
    while not STOP.is_set():
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            STOP.wait(0.1)
    return False


def watch_owner():
    # The app alone keeps the write end open. EOF also covers crashes/SIGKILL,
    # without PID reuse races or polling an unrelated process.
    while sys.stdin.buffer.read(1):
        pass
    STOP.set()


def stop_processes(processes, grace=8):
    for _, child in reversed(processes):
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    for _, child in reversed(processes):
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # A group leader can exit before a model/browser descendant. Always clean
    # the owned group, even when wait() already reaped its leader.
    for _, child in reversed(processes):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if os.environ.get("HORIZON_APP_OWNS_SERVICES") == "1":
        threading.Thread(target=watch_owner, daemon=True).start()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    logs = RUNTIME / "logs"
    logs.mkdir(exist_ok=True)
    lock = (RUNTIME / "services.lock").open("a+")
    if not acquire_lock(lock):
        lock.close()
        if STOP.is_set():
            return 0
        print("JAIS Voice services are already running in another instance.", file=sys.stderr)
        return 2
    if STOP.is_set():
        lock.close()
        return 0
    env = os.environ.copy()
    # Validate the whole selected pair before starting any local service.
    # An explicit environment path takes precedence over the daily app selector.
    selected_manifest = resolve_model_profile(RUNTIME, env)
    env["HORIZON_MODEL_MANIFEST"] = str(selected_manifest)
    apply_profile_reasoning(env, read_model_manifest(selected_manifest))
    print(f"Model profile: {selected_manifest}", flush=True)
    env.update({
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "backend")]),
        "HORIZON_PROJECT_ROOT": str(ROOT),
        "HORIZON_RUNTIME_DIR": str(RUNTIME),
        "HF_HOME": str(RUNTIME / "huggingface"),
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONUNBUFFERED": "1",
        "PATH": os.pathsep.join([str(ROOT / ".venv/bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
    })
    python = str(ROOT / ".venv/bin/python")
    asr_root = Path(env.get("HORIZON_ASR_MODEL_DIR", ROOT / ".runtime/models/JAIS-ASR-MLX"))
    commands = [
        ("asr", [str(asr_root / ".venv/bin/python"), str(ROOT / "backend/horizon_voice/local_asr.py")]),
        ("mlx", [python, "-m", "k2_runtime.gateway", "--host", "127.0.0.1", "--port", "8788"]),
    ]
    for port in (8788, 8790):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Le port local {port} est déjà occupé ; aucun processus existant n'a été arrêté.")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    processes = []
    streams = []
    try:
        for name, command in commands:
            if STOP.is_set():
                break
            stream = (logs / f"{name}.log").open("a", buffering=1)
            streams.append(stream)
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append((name, child))
            print(f"{name}: started ({child.pid})", flush=True)
        (RUNTIME / "services.json").write_text(json.dumps({"supervisor": os.getpid(), "children": {name: child.pid for name, child in processes}}))
        while not STOP.wait(0.5):
            for name, child in processes:
                if child.poll() is not None:
                    raise RuntimeError(f"Le service {name} s'est arrêté (code {child.returncode}). Voir {logs / (name + '.log')}")
    finally:
        stop_processes(processes)
        for stream in streams:
            stream.close()
        (RUNTIME / "services.json").unlink(missing_ok=True)
        lock.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        sys.exit(1)
