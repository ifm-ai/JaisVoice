"""Persistent, offline JAIS-ASR F5 worker; runs in the model's isolated Python env."""
from __future__ import annotations

import base64
import gc
import concurrent.futures
import dataclasses
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_WAV_BYTES = 16000 * 2 * 30 + 4096
LANGUAGES = {None, "", "ar", "msa", "fr", "en", "es", "it"}


def decode_audio(payload):
    if not isinstance(payload, dict) or payload.get("language") not in LANGUAGES:
        raise ValueError("Langue de transcription invalide.")
    audio = base64.b64decode(payload["audio"], validate=True)
    if len(audio) > MAX_WAV_BYTES:
        raise ValueError("L’enregistrement dépasse 30 secondes.")
    with wave.open(io.BytesIO(audio), "rb") as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("Audio attendu : WAV PCM16 mono 16 kHz.")
        count = stream.getnframes()
        if not 1600 <= count <= 480000:
            raise ValueError("Durée audio invalide.")
        frames = stream.readframes(count)
        if len(frames) != count * 2:
            raise ValueError("Audio incomplet.")
    return frames, payload.get("language") or None


def transcribe_pinned(runtime, waveform, language):
    """Same F5 generation loop, with the source processor's official LanguagePin.

    Automatic mode uses the unmodified published runtime. A language pin keeps
    the model's abstention branch, unlike prefilling a language tag in the prompt.
    """
    if language is None:
        return runtime.transcribe(waveform, 16000)
    import mlx.core as mx
    import numpy as np
    import torch
    from transformers import RepetitionPenaltyLogitsProcessor
    batch = runtime.processor(text=[runtime.prompt], audio=[waveform], sampling_rate=16000, return_tensors="pt")
    ids = batch["input_ids"][0].tolist()
    if len(ids) > 4096 - 256 or not bool((batch["attention_mask"] == 1).all()):
        raise ValueError("L’audio dépasse le contexte du modèle.")
    with torch.inference_mode():
        merged = runtime.frontend.model(**batch)
    prefix = mx.array(merged.detach().float().numpy())
    cache = runtime.decoder.make_cache()
    for first in range(0, len(ids), 256):
        scores = runtime.decoder(input_embeddings=prefix[:, first:first + 256, :], cache=cache).logits
        mx.eval(scores)
    tokens = []
    penalty = RepetitionPenaltyLogitsProcessor(1.05)
    pin = runtime.processor.language_pin(language)
    for _ in range(256):
        logits = torch.from_numpy(np.array(scores[0, -1, :].astype(mx.float32), copy=True)).reshape(1, -1)
        logits = penalty(torch.tensor([ids + tokens]), logits)
        pin.apply(tokens, logits[0])
        token = int(logits.argmax(-1)[0])
        tokens.append(token)
        if token in (1, 250019) or len(tokens) == 256:
            break
        scores = runtime.decoder(inputs=mx.array([[token]], dtype=mx.int32), cache=cache).logits
        mx.eval(scores)
    transcript = runtime.processor.post_process_transcription(torch.tensor([ids + tokens]), input_lengths=len(ids))[0]
    return {"transcription": dataclasses.asdict(transcript), "truncated": transcript.truncated,
            "reached_max_new_tokens": len(tokens) == 256, "ended_with_eos": tokens[-1] in (1, 250019)}


def normalize_result(row):
    transcript = row.get("transcription", {})
    return {"text": row.get("text") or transcript.get("text", ""),
            "refused": bool(row.get("refused") or transcript.get("refused")),
            "truncated": bool(row.get("truncated") or transcript.get("truncated")
                              or row.get("reached_max_new_tokens") or row.get("ended_with_eos") is False),
            "language": transcript.get("language"), "profile": "F5"}


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
        root = self.model_dir
        for key, value in {"HF_HOME": root / ".cache/huggingface", "HF_MODULES_CACHE": root / ".cache/huggingface/modules",
                           "NUMBA_CACHE_DIR": root / ".cache/numba", "TORCH_HOME": root / ".cache/torch"}.items():
            os.environ[key] = str(value)
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
        sys.path.insert(0, str(root / "runtime"))
        spec = root / "profiles/F5.json"
        factory = importlib.import_module("scripts.mlx_pareto_ext.jais_runtime").load_runtime
        self.runtime = factory(spec, device="gpu", threads=2)
        import mlx.core as mx
        mx.synchronize()
        logging.info("JAIS-ASR F5 loaded locally")

    def health(self):
        if not self.loading.done():
            return {"status": "loading", "message": "Chargement de JAIS-ASR F5…", "profile": "F5"}
        if self.loading.exception() is not None:
            return {"status": "error", "message": "Échec du chargement de JAIS-ASR F5. Consultez le journal ASR.", "profile": "F5"}
        if self.runtime is None:
            return {"status": "unloaded", "message": "JAIS-ASR F5 déchargé", "profile": "F5"}
        return {"status": "ready", "message": "JAIS-ASR F5 prêt", "profile": "F5"}

    def transcribe(self, frames, language):
        import mlx.core as mx
        import numpy as np
        waveform = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        try:
            return normalize_result(transcribe_pinned(self.runtime, waveform, language))
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
                    return self.respond(413, {"error": "Audio trop volumineux"})
                self.connection.settimeout(15)
                frames, language = decode_audio(json.loads(self.rfile.read(length)))
            except (ValueError, TypeError, KeyError, EOFError, wave.Error):
                return self.respond(400, {"error": "Audio ou langue invalide"})
            if not worker.slot.acquire(blocking=False):
                return self.respond(429, {"error": "Transcription locale déjà en cours"})
            try:
                if worker.health()["status"] != "ready":
                    return self.respond(503, worker.health())
                result = worker.executor.submit(worker.transcribe, frames, language).result()
                self.respond(200, result)
            except Exception:
                logging.exception("Local transcription failed")
                self.respond(500, {"error": "Échec de la transcription locale"})
            finally:
                worker.slot.release()
    return Handler


def main():
    logging.basicConfig(level=logging.INFO)
    root = Path(__file__).resolve().parents[2]
    directory = Path(os.environ.get("HORIZON_ASR_MODEL_DIR", root / ".runtime/models/JAIS-ASR-MLX"))
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
