"""One resident MLX worker, two models, cancellable OpenAI-style HTTP gateway."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import gc
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .protocol import K2Parser, normalize_messages, tool_call_deltas

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime"))
logger = logging.getLogger("horizon.mlx")
MODEL_ALIASES = ("k2-cleaner", "k2-agent")


def model_size(entry: dict) -> str | None:
    """Recognize the advertised checkpoint size, never infer it from its role."""
    match = re.search(r"(?<![\w.])(0\.9|1|4)B\b", entry.get("display_name", ""), re.IGNORECASE)
    return ("1B" if match[1] in {"0.9", "1"} else "4B") if match else None


def model_manifest_path() -> Path:
    """A profile is explicit; a failed profile never falls back to other weights."""
    return Path(os.environ.get("HORIZON_MODEL_MANIFEST", RUNTIME / "models" / "manifest.json")).expanduser().resolve()


def read_model_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or any(alias not in manifest for alias in MODEL_ALIASES):
        raise ValueError("Model manifest must contain k2-cleaner and k2-agent")
    entries = {}
    for alias in MODEL_ALIASES:
        value = manifest[alias]
        if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not value["path"].strip():
            raise ValueError(f"Missing local model path for {alias}")
        entry = dict(value)
        entry.setdefault("loader", "horizon-native-mlx-v1")
        entry.setdefault("protocol", "ifm-k2-v1")
        if entry["loader"] != "horizon-native-mlx-v1" or entry["protocol"] != "ifm-k2-v1":
            raise ValueError(f"Unsupported model loader or protocol for {alias}")
        entry.setdefault("display_name", entry.get("repo") or alias)
        if not isinstance(entry["display_name"], str) or not entry["display_name"].strip():
            raise ValueError(f"Invalid display_name for {alias}")
        entry.setdefault("context_limit", 4096 if alias == "k2-cleaner" else 16384)
        if type(entry["context_limit"]) is not int or not 256 <= entry["context_limit"] <= 131072:
            raise ValueError(f"context_limit for {alias} must be an integer from 256 to 131072")
        entry.setdefault("reasoning_efforts", ["low", "medium", "high"])
        efforts = entry["reasoning_efforts"]
        if not isinstance(efforts, list) or not efforts or any(not isinstance(effort, str) or effort not in {"low", "medium", "high"} for effort in efforts):
            raise ValueError(f"Invalid reasoning_efforts for {alias}")
        entry.setdefault("default_reasoning_effort", "low" if alias == "k2-cleaner" else "medium")
        if not isinstance(entry["default_reasoning_effort"], str) or entry["default_reasoning_effort"] not in efforts:
            raise ValueError(f"default_reasoning_effort must be one of reasoning_efforts for {alias}")
        for key, default, allowed in (
            ("tool_call_format", "xml", {"json", "xml", "xml_typed"}),
            ("tool_presentation_format", "markdown", {"json", "xml", "markdown"}),
        ):
            entry.setdefault(key, default)
            if not isinstance(entry[key], str) or entry[key] not in allowed:
                raise ValueError(f"Invalid {key} for {alias}")
        model_path = Path(entry["path"]).expanduser()
        if not model_path.is_absolute():
            model_path = path.parent / model_path
        elif not model_path.exists():
            # Keep the previous bundle-relocation behavior, scoped to this profile.
            relocated = path.parent / model_path.name
            if relocated.exists():
                model_path = relocated
        if not model_path.is_dir() or not (model_path / "config.json").is_file():
            raise ValueError(f"Prepared model directory is missing for {alias}: {model_path}")
        entry["path"] = str(model_path.resolve())
        entries[alias] = entry
    return entries


def prepare_k2_prompt(tokenizer, messages, tools, effort: str, entry: dict):
    """Respect the checkpoint's native prefill, including preclosed low thinking.

    Some K2 exports include the empty low-effort thinking block in the prompt;
    older exports leave it open. Infer only this explicit, validated suffix,
    never infer a missing reasoning delimiter from generated content.
    """
    if effort not in {"low", "medium", "high"} or effort not in entry.get("reasoning_efforts", ["low", "medium", "high"]):
        raise ValueError(f"Unsupported reasoning_effort for this model: {effort}")
    rendered = tokenizer.apply_chat_template(messages, tools=tools or None, tokenize=False,
        add_generation_prompt=True, reasoning_effort=effort,
        tool_call_format=entry.get("tool_call_format", "xml"),
        tool_presentation_format=entry.get("tool_presentation_format", "markdown"))
    opening = {"low": "ifm|think_faster", "medium": "ifm|think_fast", "high": "ifm|think"}[effort]
    suffix = rendered.rsplit("<|ifm|im_start|>assistant\n", 1)
    pattern = rf"<{re.escape(opening)}>\s*(?P<closed></{re.escape(opening)}>)?\s*"
    match = re.fullmatch(pattern, suffix[-1]) if len(suffix) == 2 else None
    if match is None:
        raise ValueError("Unsupported K2 generation prefix; refusing to guess reasoning framing")
    parser = K2Parser(tools, effort)
    if match.group("closed"):
        parser.mode = "content"
    return tokenizer.encode(rendered, add_special_tokens=False), parser


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str
    model_size: Literal["1B", "4B"] | None = None
    messages: list[dict] = Field(min_length=1, max_length=100)
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    stream: bool = False
    max_tokens: int = Field(default=2048, ge=1, le=4096)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=4096)
    temperature: float = Field(default=0.0, ge=0, le=2)
    top_p: float = Field(default=0.95, gt=0, le=1)
    reasoning_effort: str = "low"
    debug_trace: bool = False


class Worker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="horizon-mlx")
        self.models = {}
        self._state_lock = threading.Lock()
        self._model_health = {}
        self.manifest_path = model_manifest_path()
        self.status, self.message = "starting", "Chargement du modèle 4B…"
        self.lifecycle_busy = False
        self.pending = 0
        self.active_flags = set()
        self.active_request = None

    def health_snapshot(self):
        # Loading and inference run on the executor. Never hold this lock while
        # reading weights, compiling kernels, or waiting for Metal evaluation.
        with self._state_lock:
            return {"status": self.status, "message": self.message,
                    "model_manifest": str(self.manifest_path),
                    "models": {alias: {**deepcopy(entry), "model_size": model_size(entry)}
                               for alias, entry in self._model_health.items()}, "pending": self.pending,
                    "active_request": dict(self.active_request) if self.active_request else None}

    def model_catalog(self):
        # Preserve the OpenAI model-list contract: only warmed-up loaded models.
        with self._state_lock:
            return {"object": "list", "data": [
                {"id": alias, "object": "model", "created": 0, "owned_by": "local",
                 "display_name": item[3].get("display_name", alias)}
                for alias, item in self.models.items()
            ]}

    def load(self):
        try:
            manifest = read_model_manifest(self.manifest_path)
            with self._state_lock:
                self._model_health = {
                    alias: {**entry, "status": "waiting"}
                    for alias, entry in manifest.items()
                }
                for entry in self._model_health.values():
                    entry.pop("error", None)
            from .model import load_model
            import mlx.core as mx
            for alias in MODEL_ALIASES:
                entry = manifest[alias]
                if model_size(entry) != "4B":
                    with self._state_lock:
                        self._model_health[alias]["status"] = "unloaded"
                    continue
                path = Path(entry["path"])
                with self._state_lock:
                    self.message = f"Chargement {entry['display_name']}…"
                    self._model_health[alias]["status"] = "loading"
                model, tokenizer, config = load_model(path)
                maximum = config.get("max_position_embeddings")
                if isinstance(maximum, int) and entry["context_limit"] > maximum:
                    raise ValueError(f"Configured context exceeds the checkpoint limit for {alias}")
                for effort in entry["reasoning_efforts"]:
                    prepare_k2_prompt(tokenizer, [{"role": "user", "content": "Ready?"}], [], effort, entry)
                mx.eval(model(mx.array([[0, 1]])))
                with self._state_lock:
                    self.models[alias] = (model, tokenizer, config, entry)
                    self._model_health[alias]["status"] = "ready"
                logger.info("Loaded %s; active memory %.2f GB", alias, mx.get_active_memory() / 1e9)
            with self._state_lock:
                self.status, self.message = "ready", "Prêt · " + " + ".join(entry[3]["display_name"] for entry in self.models.values())
        except Exception as exc:
            logger.exception("MLX model loading failed")
            with self._state_lock:
                self.status, self.message = "error", str(exc)
                for entry in self._model_health.values():
                    if entry["status"] not in {"ready", "unloaded"}:
                        entry["error"] = str(exc) if entry["status"] == "loading" else "Chargement interrompu : " + str(exc)
                        entry["status"] = "error"

    def change_model(self, size, action):
        """Runs on the same single executor as inference; no Metal work races."""
        aliases = [alias for alias, entry in self._model_health.items() if model_size(entry) == size]
        if len(aliases) != 1:
            raise ValueError(f"Le modèle {size} n’est pas disponible dans ce profil.")
        alias = aliases[0]
        import mlx.core as mx
        if action == "unload":
            with self._state_lock:
                self.models.pop(alias, None)
            gc.collect()
            mx.clear_cache()
            with self._state_lock:
                self._model_health[alias].update(status="unloaded")
                self._model_health[alias].pop("error", None)
            return
        for other in list(self.models):
            if other != alias:
                self.change_model(model_size(self._model_health[other]), "unload")
        if alias in self.models:
            return
        with self._state_lock:
            entry = dict(self._model_health[alias])
            self._model_health[alias].update(status="loading")
            self._model_health[alias].pop("error", None)
        try:
            from .model import load_model
            model, tokenizer, config = load_model(Path(entry["path"]))
            maximum = config.get("max_position_embeddings")
            if isinstance(maximum, int) and entry["context_limit"] > maximum:
                raise ValueError(f"Configured context exceeds the checkpoint limit for {alias}")
            for effort in entry["reasoning_efforts"]:
                prepare_k2_prompt(tokenizer, [{"role": "user", "content": "Ready?"}], [], effort, entry)
            mx.eval(model(mx.array([[0, 1]])))
            with self._state_lock:
                self.models[alias] = (model, tokenizer, config, entry)
                self._model_health[alias].update(status="ready")
        except Exception as exc:
            with self._state_lock:
                self._model_health[alias].update(status="error", error=str(exc))
            raise

    def resolve_model(self, body):
        original = self.models.get(body.model)
        if body.model_size is None:
            if original is None:
                raise ValueError("Le modèle demandé n’est pas disponible : chargez-le dans les réglages.")
            return original
        candidates = [item for item in self.models.values() if model_size(item[3]) == body.model_size]
        if len(candidates) != 1:
            raise ValueError(f"Le modèle {body.model_size} n’est pas disponible dans le profil chargé.")
        model, tokenizer, config, entry = candidates[0]
        # Role context survives unloading the checkpoint originally assigned to it.
        role_entry = self._model_health.get(body.model, original[3] if original else {})
        limit = role_entry.get("context_limit", 4096 if body.model == "k2-cleaner" else 16384)
        entry = {**entry, "context_limit": min(limit, config.get("max_position_embeddings", limit))}
        return model, tokenizer, config, entry

    def run(self, body, canceled, emit):
        raw_parts = []
        trace_enabled = os.environ.get("HORIZON_MLX_DIAGNOSTICS") == "1" or (body is not None and body.debug_trace)
        try:
            if canceled.is_set():
                return
            from mlx_lm import stream_generate
            from mlx_lm.sample_utils import make_sampler
            import mlx.core as mx
            model, tokenizer, config, entry = self.resolve_model(body)
            with self._state_lock:
                self.active_request = {"model": body.model, "model_size": model_size(entry), "stage": "prefill", "started_at": time.time(), "generated_tokens": 0}
            messages = normalize_messages(body.messages)
            tools = body.tools or []
            if body.tool_choice == "none":
                tools = []
            if isinstance(body.tool_choice, dict):
                required = body.tool_choice.get("function", {}).get("name")
                tools = [tool for tool in tools if tool["function"]["name"] == required]
                if not tools:
                    raise ValueError("Requested tool_choice is not declared")
                messages.insert(0, {"role": "system", "content": f"Call the {required} tool for this turn."})
            elif body.tool_choice == "required":
                messages.insert(0, {"role": "system", "content": "Use one of the provided tools for this turn."})
            if body.model == "k2-cleaner" and tools:
                raise ValueError("The transcript cleaner cannot use tools")
            tokens, parser = prepare_k2_prompt(tokenizer, messages, tools, body.reasoning_effort, entry)
            max_tokens = body.max_completion_tokens or body.max_tokens
            context_limit = entry.get("context_limit", 4096 if body.model == "k2-cleaner" else 16384)
            if len(tokens) + max_tokens > context_limit:
                raise ValueError(f"Context budget exceeded: {len(tokens)} input + {max_tokens} output > {context_limit}")
            emit(("ready", {"prompt_tokens": len(tokens)}))
            started = time.perf_counter()
            def progress(done, total):
                if canceled.is_set():
                    raise InterruptedError("Request cancelled during prompt processing")
            generator = stream_generate(model, tokenizer, tokens, max_tokens=max_tokens,
                sampler=make_sampler(temp=body.temperature, top_p=body.top_p),
                prefill_step_size=256, prompt_progress_callback=progress)
            last = None
            try:
                for chunk in generator:
                    if canceled.is_set():
                        return
                    last = chunk
                    if trace_enabled:
                        raw_parts.append(chunk.text)
                    for delta in parser.feed(chunk.text):
                        if "tool_calls" not in delta:
                            emit(("delta", delta))
                    with self._state_lock:
                        self.active_request.update(stage=parser.mode, generated_tokens=chunk.generation_tokens)
            finally:
                generator.close()
            finish = parser.finish(last.finish_reason if last else "length")
            if body.tool_choice == "required" or isinstance(body.tool_choice, dict):
                if not parser.calls:
                    raise ValueError("K2 did not produce the required tool call")
            if parser.calls:
                for delta in tool_call_deltas(parser.calls):
                    emit(("delta", delta))
            emit(("done", {"finish_reason": finish, "content": parser.content, "reasoning_content": parser.reasoning,
                "tool_calls": [{k: v for k, v in call.items() if k != "index"} for call in parser.calls],
                "usage": {"prompt_tokens": len(tokens), "completion_tokens": last.generation_tokens,
                          "total_tokens": len(tokens) + last.generation_tokens},
                "metrics": {"elapsed_ms": round((time.perf_counter() - started) * 1000),
                            "generation_tps": last.generation_tps, "peak_memory_gb": mx.get_peak_memory() / 1e9}}))
        except InterruptedError:
            pass
        except Exception as exc:
            logger.warning("Generation failed: %s", exc)
            emit(("error", str(exc)))
        finally:
            if trace_enabled and body is not None:
                trace_dir = RUNTIME / "validation" / "mlx-diagnostics"
                trace_dir.mkdir(parents=True, exist_ok=True)
                trace = {"request": body.model_dump(), "generated_text": "".join(raw_parts), "state": self.active_request}
                (trace_dir / f"{body.model}-{time.time_ns()}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n")
            with self._state_lock:
                self.active_request = None
            emit(("end", None))


worker = Worker()


@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()
    loading = loop.run_in_executor(worker.executor, worker.load)
    yield
    for flag in list(worker.active_flags):
        flag.set()
    worker.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Horizon local K2 gateway", lifespan=lifespan)


@app.get("/health")
async def health():
    return worker.health_snapshot()


@app.get("/v1/models")
async def models():
    return worker.model_catalog()


class ModelMemoryRequest(BaseModel):
    action: Literal["load", "unload"]


@app.post("/models/{size}/memory")
async def model_memory(size: Literal["1B", "4B"], body: ModelMemoryRequest):
    # Reserve before yielding so concurrent HTTP requests cannot race inference.
    if worker.status == "starting" or worker.lifecycle_busy or worker.pending or worker.active_request:
        raise HTTPException(409, "Une opération est en cours. Réessayez après sa fin.")
    if not any(model_size(entry) == size for entry in worker._model_health.values()):
        raise HTTPException(404, "Ce modèle n’est pas disponible dans ce profil.")
    worker.lifecycle_busy = True
    async def perform():
        try:
            await asyncio.get_running_loop().run_in_executor(worker.executor, worker.change_model, size, body.action)
            with worker._state_lock:
                worker.status, worker.message = "ready", "Services prêts"
        finally:
            worker.lifecycle_busy = False
    # Cancellation of an HTTP client must not release the reservation before Metal finishes.
    task = asyncio.create_task(perform())
    try:
        await asyncio.shield(task)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return worker.health_snapshot()


@app.post("/v1/chat/completions")
async def completions(body: CompletionRequest, request: Request):
    if worker.lifecycle_busy:
        raise HTTPException(409, "La mémoire des modèles est en cours de modification.")
    if worker.status != "ready":
        raise HTTPException(503, worker.message)
    if body.model not in worker.models and body.model not in worker._model_health:
        raise HTTPException(404, "Unknown model alias")
    if worker._model_health:
        try:
            worker.resolve_model(body)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if worker.pending >= 8:
        raise HTTPException(429, "Local generation queue is full")
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    canceled = threading.Event()
    worker.active_flags.add(canceled)
    worker.pending += 1
    def emit(event):
        if not canceled.is_set():
            loop.call_soon_threadsafe(queue.put_nowait, event)
    future = loop.run_in_executor(worker.executor, worker.run, body, canceled, emit)
    identifier, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
    def release():
        if canceled in worker.active_flags:
            canceled.set()
            worker.active_flags.discard(canceled)
            worker.pending -= 1
    async def next_event():
        while True:
            if await request.is_disconnected():
                raise asyncio.CancelledError()
            try:
                return await asyncio.wait_for(queue.get(), timeout=0.15)
            except asyncio.TimeoutError:
                continue
    def chunk(delta, finish=None):
        return {"id": identifier, "object": "chat.completion.chunk", "created": created,
                "model": body.model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    try:
        first, metadata = await next_event()
        if first != "ready":
            raise HTTPException(400, metadata or "Cancelled")
    except BaseException:
        release()
        raise
    if body.stream:
        async def events():
            try:
                yield "data: " + json.dumps(chunk({"role": "assistant"})) + "\n\n"
                while True:
                    kind, data = await next_event()
                    if kind == "delta":
                        yield "data: " + json.dumps(chunk(data), ensure_ascii=False) + "\n\n"
                    elif kind == "done":
                        final = chunk({}, data["finish_reason"])
                        final["usage"] = data["usage"]
                        yield "data: " + json.dumps(final) + "\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    elif kind == "error":
                        yield "data: " + json.dumps({"error": {"message": data, "type": "generation_error", "code": "incomplete_generation"}}) + "\n\n"
                        return
                    elif kind == "end":
                        return
            finally:
                release()
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
    try:
        while True:
            kind, data = await next_event()
            if kind == "error":
                raise HTTPException(422, data)
            if kind == "done":
                message = {"role": "assistant", "content": data["content"], "reasoning_content": data["reasoning_content"]}
                if data["tool_calls"]:
                    message["tool_calls"] = data["tool_calls"]
                return {"id": identifier, "object": "chat.completion", "created": created, "model": body.model,
                        "choices": [{"index": 0, "message": message, "finish_reason": data["finish_reason"]}],
                        "usage": data["usage"], "metrics": data["metrics"]}
            if kind == "end":
                raise HTTPException(422, "Generation ended without a final answer")
    finally:
        release()


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
