#!/usr/bin/env python3
"""Own and supervise the model services (ASR, MLX gateway) for one JAIS Voice instance.

The gateway always runs. The ASR service runs while its model is installed:
it starts after a download and stops after a deletion, without a restart.
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
from jais_runtime import model_store

RUNTIME = Path(os.environ.get("JAIS_RUNTIME_DIR", ROOT / ".runtime"))
STOP = threading.Event()


def shutdown(signum=None, frame=None):
    STOP.set()


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
    if os.environ.get("JAIS_APP_OWNS_SERVICES") == "1":
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
    # An invalid catalog stops startup before any service runs.
    base = model_store.models_dir(RUNTIME)
    asr = next((entry for entry in model_store.load_catalog() if entry["kind"] == "asr"), None)
    env.update({
        "PYTHONPATH": str(ROOT),
        "JAIS_PROJECT_ROOT": str(ROOT),
        "JAIS_RUNTIME_DIR": str(RUNTIME),
        "HF_HOME": str(RUNTIME / "huggingface"),
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONUNBUFFERED": "1",
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
    })
    # The interpreter running this supervisor: the project .venv or the one embedded in the app.
    python = sys.executable
    asr_root = model_store.model_path(asr, base) if asr else None
    if asr_root:
        env["JAIS_ASR_MODEL_DIR"] = str(asr_root)
    asr_command = [str(asr_root / ".venv/bin/python"), str(ROOT / "jais_runtime/asr_service.py")] if asr_root else None
    asr_installed = lambda: asr is not None and model_store.is_installed(asr, base)
    for port in (8788, 8790):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Local port {port} is already in use; no existing process was stopped.")
    processes = []
    streams = []

    def start(name, command):
        stream = (logs / f"{name}.log").open("a", buffering=1)
        streams.append(stream)
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append((name, child))
        print(f"{name}: started ({child.pid})", flush=True)
        (RUNTIME / "services.json").write_text(json.dumps({"supervisor": os.getpid(), "children": {name: child.pid for name, child in processes}}))

    try:
        if asr_installed() and not STOP.is_set():
            start("asr", asr_command)
        if not STOP.is_set():
            start("mlx", [python, "-m", "jais_runtime.gateway"])
        while not STOP.wait(0.5):
            running = {name: child for name, child in processes}
            if "asr" in running and not asr_installed():
                # The model was deleted: stop its service rather than report a crash.
                stop_processes([("asr", running["asr"])], grace=3)
                processes[:] = [item for item in processes if item[0] != "asr"]
                print("asr: stopped (model removed)", flush=True)
            elif "asr" not in running and asr_installed():
                start("asr", asr_command)
            for name, child in processes:
                if child.poll() is not None:
                    raise RuntimeError(f"The {name} service stopped (code {child.returncode}). See {logs / (name + '.log')}")
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
