"""Persistent, offline Jais-ASR worker (MLX), on the services' Python environment."""
from __future__ import annotations

import base64
import gc
import concurrent.futures
import dataclasses
import io
import json
import logging
import os
from pathlib import Path
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_WAV_BYTES = 16000 * 2 * 30 + 4096
LANGUAGES = {None, "", "ar", "msa", "fr", "en", "es", "it"}


def decode_audio(payload):
    if not isinstance(payload, dict) or payload.get("language") not in LANGUAGES:
        raise ValueError("Invalid transcription language.")
    audio = base64.b64decode(payload["audio"], validate=True)
    if len(audio) > MAX_WAV_BYTES:
        raise ValueError("The recording exceeds 30 seconds.")
    with wave.open(io.BytesIO(audio), "rb") as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("Expected audio: mono PCM16 WAV at 16 kHz.")
        count = stream.getnframes()
        if not 1600 <= count <= 480000:
            raise ValueError("Invalid audio duration.")
        frames = stream.readframes(count)
        if len(frames) != count * 2:
            raise ValueError("Incomplete audio.")
    return frames, payload.get("language") or None


def normalize_result(row):
    return {"text": row.get("text", ""), "refused": bool(row.get("refused")),
            "truncated": bool(row.get("truncated")), "language": row.get("language")}


class LocalASR:
    def __init__(self, model_dir):
        self.model_dir = Path(model_dir).resolve()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-metal")
        self.slot = threading.BoundedSemaphore(1)
        self.runtime = None
        self.loading = self.executor.submit(self.load)

    def change_memory(self, action):
        if action == "load":
            if self.runtime is None:
                self.load()
        else:
            import mlx.core as mx
            mx.synchronize()
            self.runtime = None
            gc.collect()
            mx.clear_cache()

    def load(self):
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
        from jais_runtime.asr_model import Transcriber
        self.runtime = Transcriber(self.model_dir)
        import mlx.core as mx
        mx.synchronize()
        logging.info("Jais-ASR loaded locally")

    def health(self):
        if not self.loading.done():
            return {"status": "loading", "message": "Loading Jais-ASR…"}
        if self.loading.exception() is not None:
            return {"status": "error", "message": "Jais-ASR failed to load. See the ASR log."}
        if self.runtime is None:
            return {"status": "unloaded", "message": "Jais-ASR unloaded"}
        return {"status": "ready", "message": "Jais-ASR ready"}

    def transcribe(self, frames, language):
        import mlx.core as mx
        import numpy as np
        waveform = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        try:
            return normalize_result(dataclasses.asdict(self.runtime(waveform, language)))
        finally:
            mx.synchronize()
            mx.clear_cache()


def make_handler(worker):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.respond(200, worker.health()) if self.path == "/health" else self.respond(404, {})

        def do_POST(self):
            if self.path == "/memory":
                if self.headers.get("Origin"):
                    return self.respond(403, {"detail": "Local clients only"})
                try:
                    self.connection.settimeout(15)
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024:
                        raise ValueError()
                    action = json.loads(self.rfile.read(length))["action"]
                    if action not in {"load", "unload"}:
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    return self.respond(400, {"detail": "Invalid memory action"})
                if not worker.slot.acquire(blocking=False):
                    return self.respond(409, {"detail": "ASR is busy"})
                try:
                    worker.loading = worker.executor.submit(worker.change_memory, action)
                    worker.loading.result()
                    return self.respond(200, worker.health())
                except Exception:
                    logging.exception("ASR memory operation failed")
                    return self.respond(500, {"detail": "ASR memory operation failed"})
                finally:
                    worker.slot.release()
            if self.path != "/transcribe":
                return self.respond(404, {})
            if worker.health()["status"] != "ready":
                return self.respond(503, worker.health())
            # Local service only; reject browser cross-origin requests.
            if self.headers.get("Origin"):
                return self.respond(403, {"error": "Local clients only"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_WAV_BYTES * 2:
                    return self.respond(413, {"error": "Audio too large"})
                self.connection.settimeout(15)
                frames, language = decode_audio(json.loads(self.rfile.read(length)))
            except (ValueError, TypeError, KeyError, EOFError, wave.Error):
                return self.respond(400, {"error": "Invalid audio or language"})
            if not worker.slot.acquire(blocking=False):
                return self.respond(429, {"error": "A local transcription is already running"})
            try:
                if worker.health()["status"] != "ready":
                    return self.respond(503, worker.health())
                result = worker.executor.submit(worker.transcribe, frames, language).result()
                self.respond(200, result)
            except Exception:
                logging.exception("Local transcription failed")
                self.respond(500, {"error": "Local transcription failed"})
            finally:
                worker.slot.release()
    return Handler


def main():
    logging.basicConfig(level=logging.INFO)
    directory = os.environ.get("JAIS_ASR_MODEL_DIR")
    if not directory:
        from jais_runtime import model_store
        asr = next(entry for entry in model_store.load_catalog() if entry["kind"] == "asr")
        directory = model_store.model_path(asr, model_store.models_dir())
    worker = LocalASR(directory)
    worker.loading.add_done_callback(lambda future: logging.error("ASR load failed: %s", future.exception()) if future.exception() else None)
    server = ThreadingHTTPServer(("127.0.0.1", 8790), make_handler(worker))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        worker.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
