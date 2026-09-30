"""Opt-in transport integration: real LiveKit, explicit ASR/LLM test doubles.

Run from the project root with .venv/bin/python backend/tests/bootstrap_check.py.
This never loads MLX weights or controls a browser page.
"""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

ROOT = Path(__file__).resolve().parents[2]


class ExplicitInferenceTestDouble(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def reply(self, data, code=200, kind="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(json.dumps({"status": "ready", "message": "EXPLICIT TEST DOUBLE", "models": {}}).encode())

    def do_POST(self):
        content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/asr":
            self.reply(json.dumps({"text": "Paris, je rectifie, Lyon", "refused": False, "truncated": False}).encode())
            return
        body = json.loads(content)
        if body["model"] == "k2-cleaner":
            self.reply(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "Lyon", "reasoning_content": "PRIVATE TEST REASONING"}}]}).encode())
            return
        user = [message["content"] for message in body["messages"] if message["role"] == "user"]
        if user != ["Lyon"]:
            self.reply(json.dumps({"error": "Uncleaned text entered the agent"}).encode(), 400)
            return
        def chunk(delta, finish=None):
            return {"id": "explicit-test", "model": "k2-agent", "object": "chat.completion.chunk", "created": 0, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        chunks = [chunk({"reasoning_content": "PRIVATE TEST REASONING"}), chunk({"content": "EXPLICIT TRANSPORT TEST: Lyon"}), chunk({}, "stop")]
        self.reply(("".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n").encode(), kind="text/event-stream")


async def run():
    test_server = ThreadingHTTPServer(("127.0.0.1", 17888), ExplicitInferenceTestDouble)
    threading.Thread(target=test_server.serve_forever, daemon=True).start()
    environment = dict(os.environ)
    environment.update({
        "PYTHONPATH": str(ROOT / "backend") + os.pathsep + str(ROOT),
        "LIVEKIT_URL": "ws://127.0.0.1:17880", "LIVEKIT_API_KEY": "devkey", "LIVEKIT_API_SECRET": "secret",
        "HORIZON_ASR_HEALTH_URL": "http://127.0.0.1:17888/health",
        "HORIZON_MLX_URL": "http://127.0.0.1:17888", "HORIZON_ASR_URL": "http://127.0.0.1:17888/asr",
        "HORIZON_AGENT_HEALTH_PORT": "17889", "HORIZON_AGENT_HEALTH_URL": "http://127.0.0.1:17889/",
        "HORIZON_PROJECT_ROOT": str(ROOT),
    })
    processes, handles = [], []
    with tempfile.TemporaryDirectory(prefix="bootstrap-test-", dir=ROOT / ".runtime") as directory:
        scratch = Path(directory)
        try:
            commands = [
                [str(ROOT / ".runtime/bin/livekit-server"), "--dev", "--bind", "127.0.0.1", "--node-ip", "127.0.0.1", "--config-body", "port: 17880\nlogging:\n  level: warn\nrtc:\n  tcp_port: 17881\n  udp_port: 17882\n  use_external_ip: false\n"],
                [sys.executable, "-m", "horizon_voice.agent", "start"],
                [sys.executable, "-m", "uvicorn", "horizon_voice.control:app", "--host", "127.0.0.1", "--port", "17887"],
            ]
            for index, command in enumerate(commands):
                log = (scratch / f"service-{index}.log").open("w+")
                handles.append(log)
                processes.append(subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT))
            async with httpx.AsyncClient(trust_env=False) as http:
                for _ in range(100):
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError("A bootstrap service exited")
                    try:
                        response = await http.get("http://127.0.0.1:17887/health", timeout=3)
                        if response.json()["status"] == "ready":
                            break
                    except (httpx.HTTPError, ValueError): pass
                    await asyncio.sleep(.3)
                else:
                    raise RuntimeError("Isolated services never became ready")
            wav_path = scratch / "explicit-silent-test.wav"
            with wave.open(str(wav_path), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000)
            smoke = await asyncio.create_subprocess_exec(sys.executable, "-m", "horizon_voice.smoke", "--file", str(wav_path), "--control-url", "http://127.0.0.1:17887", "--timeout", "45", cwd=ROOT, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await asyncio.wait_for(smoke.communicate(), 60)
            print(output.decode())
            if smoke.returncode != 0 or b'"complete"' not in output or b'EXPLICIT TRANSPORT TEST: Lyon' not in output or b'PRIVATE TEST REASONING' in output:
                raise RuntimeError("Explicit transport check failed")
            print("PASS: real LiveKit dispatch + WAV + cleaned-only agent input + reliable events. ASR and LLM were explicit test doubles.")
        except BaseException:
            for index, log in enumerate(handles):
                log.flush(); log.seek(0)
                print(f"Service {index} log:\n" + log.read()[-18000:])
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None: process.terminate()
            for process in reversed(processes):
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            for handle in handles: handle.close()
            test_server.shutdown(); test_server.server_close()


if __name__ == "__main__":
    asyncio.run(run())
