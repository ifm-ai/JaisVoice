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
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import model_store
from .cleanup_guard import cleanup_problem
from .prompts import cleaner_messages
from .protocol import K2Parser, normalize_messages, tool_call_deltas

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get("HORIZON_RUNTIME_DIR", ROOT / ".runtime"))
logger = logging.getLogger("horizon.mlx")
MODEL_ROLES = ("k2-cleaner", "k2-agent")
# Agent model IDs for OpenAI clients that select a size by model name (Pi).
AGENT_MODEL_IDS = {f"k2-{size.lower()}": size for size in model_store.SIZES}
DEFAULT_SIZE = "4B"


def role_effort(entry: dict, role: str) -> str:
    """An explicit environment override, otherwise the model's catalog default."""
    variable = "HORIZON_CLEANER_REASONING_EFFORT" if role == "k2-cleaner" else "HORIZON_AGENT_REASONING_EFFORT"
    return os.environ.get(variable) or entry["cleaner_reasoning_effort" if role == "k2-cleaner" else "agent_reasoning_effort"]


def read_inventory() -> dict:
    """Catalog language models by size, with local path and installation state.

    Read again on demand: downloads and deletions happen outside this process.
    """
    base = model_store.models_dir(RUNTIME)
    return {entry["size"]: {**entry, "path": str(model_store.model_path(entry, base)), "installed": model_store.is_installed(entry, base)}
            for entry in model_store.load_catalog() if entry["kind"] == "llm"}


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
    model_size: Literal["1B", "4B", "7B"] | None = None
    messages: list[dict] = Field(min_length=1, max_length=256)
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
        self.models = {}  # size -> (model, tokenizer, config, entry)
        self._state_lock = threading.Lock()
        self._model_health = {}  # size -> catalog entry and status
        self.status, self.message = "starting", "Chargement des modèles…"
        self.lifecycle_busy = False
        self.pending = 0
        self.active_flags = set()
        self.active_request = None

    def refresh_inventory(self):
        inventory = read_inventory()
        with self._state_lock:
            previous, self._model_health = self._model_health, {}
            for size, entry in inventory.items():
                before = previous.get(size, {})
                if size in self.models:
                    status = "ready"
                elif not entry["installed"]:
                    status = "not_installed"
                else:
                    status = before.get("status") if before.get("status") in {"loading", "error"} else "unloaded"
                self._model_health[size] = {**entry, "status": status}
                if status == "error" and "error" in before:
                    self._model_health[size]["error"] = before["error"]

    def health_snapshot(self):
        # Loading and inference run on the executor. Never hold this lock while
        # reading weights, compiling kernels, or waiting for Metal evaluation.
        try:
            self.refresh_inventory()
        except (OSError, ValueError) as exc:
            with self._state_lock:
                self.status, self.message = "error", f"Catalogue des modèles invalide : {exc}"
        with self._state_lock:
            models = {size: {"model_size": size, "display_name": entry["display_name"], "status": entry["status"],
                             "installed": entry["installed"], "downloadable": entry["download"] is not None,
                             "cleaner_reasoning_effort": role_effort(entry, "k2-cleaner"),
                             "agent_reasoning_effort": role_effort(entry, "k2-agent"),
                             **({"error": entry["error"]} if "error" in entry else {})}
                      for size, entry in self._model_health.items()}
            return {"status": self.status, "message": self.message, "model_catalog": str(model_store.catalog_path()),
                    "models": models, "pending": self.pending,
                    "active_request": dict(self.active_request) if self.active_request else None}

    def model_catalog(self):
        # Preserve the OpenAI model-list contract: only warmed-up loaded models.
        with self._state_lock:
            return {"object": "list", "data": [
                {"id": f"k2-{size.lower()}", "object": "model", "created": 0, "owned_by": "local", "display_name": item[3]["display_name"]}
                for size, item in self.models.items()
            ]}

    def load(self):
        """Load the default size when installed, else the first installed model."""
        try:
            self.refresh_inventory()
            installed = [size for size, entry in self._model_health.items() if entry["installed"]]
            preferred = os.environ.get("HORIZON_DEFAULT_MODEL_SIZE", DEFAULT_SIZE)
            size = preferred if preferred in installed else (installed[0] if installed else None)
            if size:
                self.change_model(size, "load")
            with self._state_lock:
                self.status = "ready"
                self.message = "Prêt · " + self._model_health[size]["display_name"] if size else "Aucun modèle de langage installé"
        except Exception as exc:
            logger.exception("MLX model loading failed")
            with self._state_lock:
                self.status, self.message = "error", str(exc)

    def change_model(self, size, action):
        """Runs on the same single executor as inference; no Metal work races."""
        self.refresh_inventory()
        if size not in self._model_health:
            raise ValueError(f"Le modèle {size} n’existe pas dans le catalogue.")
        import mlx.core as mx
        if action == "unload":
            with self._state_lock:
                self.models.pop(size, None)
            gc.collect()
            mx.clear_cache()
            with self._state_lock:
                health = self._model_health[size]
                health["status"] = "unloaded" if health["installed"] else "not_installed"
                health.pop("error", None)
            return
        if not self._model_health[size]["installed"]:
            raise ValueError(f"Le modèle {size} n’est pas installé : téléchargez-le dans les réglages.")
        for other in list(self.models):
            if other != size:
                self.change_model(other, "unload")
        if size in self.models:
            return
        with self._state_lock:
            entry = dict(self._model_health[size])
            self._model_health[size]["status"] = "loading"
            self._model_health[size].pop("error", None)
        try:
            from .model import load_model
            model, tokenizer, config = load_model(Path(entry["path"]))
            maximum = config.get("max_position_embeddings")
            if isinstance(maximum, int) and entry["context_limit"] > maximum:
                raise ValueError(f"Configured context exceeds the checkpoint limit for {size}")
            for effort in entry["reasoning_efforts"]:
                prepare_k2_prompt(tokenizer, [{"role": "user", "content": "Ready?"}], [], effort, entry)
            mx.eval(model(mx.array([[0, 1]])))
            with self._state_lock:
                self.models[size] = (model, tokenizer, config, entry)
                self._model_health[size]["status"] = "ready"
            logger.info("Loaded %s; active memory %.2f GB", size, mx.get_active_memory() / 1e9)
        except Exception as exc:
            with self._state_lock:
                self._model_health[size].update(status="error", error=str(exc))
            raise

    def resolve_model(self, body):
        """The requested size (or the only loaded one), with the role's context budget."""
        size = body.model_size
        if size is None:
            if len(self.models) != 1:
                raise ValueError("Précisez la taille du modèle : aucun ou plusieurs modèles sont chargés.")
            size = next(iter(self.models))
        if size not in self.models:
            raise ValueError(f"Le modèle {size} n’est pas disponible : chargez-le dans les réglages.")
        model, tokenizer, config, entry = self.models[size]
        limit = model_store.CLEANER_CONTEXT if body.model == "k2-cleaner" else entry["context_limit"]
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
                self.active_request = {"model": body.model, "model_size": entry["size"], "stage": "prefill", "started_at": time.time(), "generated_tokens": 0}
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
async def model_memory(size: Literal["1B", "4B", "7B"], body: ModelMemoryRequest):
    # Reserve before yielding so concurrent HTTP requests cannot race inference.
    if worker.status == "starting" or worker.lifecycle_busy or worker.pending or worker.active_request:
        raise HTTPException(409, "Une opération est en cours. Réessayez après sa fin.")
    if size not in worker._model_health:
        raise HTTPException(404, "Ce modèle n’existe pas dans le catalogue.")
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


async def begin_generation(body: CompletionRequest, request: Request):
    """Queue one generation; returns its event reader and the release callback."""
    if worker.lifecycle_busy:
        raise HTTPException(409, "La mémoire des modèles est en cours de modification.")
    if worker.status != "ready":
        raise HTTPException(503, worker.message)
    if body.model not in MODEL_ROLES:
        raise HTTPException(404, "Unknown model alias")
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
    loop.run_in_executor(worker.executor, worker.run, body, canceled, emit)
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
    try:
        first, metadata = await next_event()
        if first != "ready":
            raise HTTPException(400, metadata or "Cancelled")
    except BaseException:
        release()
        raise
    return next_event, release


async def final_result(next_event):
    while True:
        kind, data = await next_event()
        if kind == "error":
            raise HTTPException(422, data)
        if kind == "done":
            return data
        if kind == "end":
            raise HTTPException(422, "Generation ended without a final answer")


@app.post("/v1/chat/completions")
async def completions(body: CompletionRequest, request: Request):
    if body.model in AGENT_MODEL_IDS:
        body.model, body.model_size = "k2-agent", AGENT_MODEL_IDS[body.model]
    next_event, release = await begin_generation(body, request)
    identifier, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
    def chunk(delta, finish=None):
        return {"id": identifier, "object": "chat.completion.chunk", "created": created,
                "model": body.model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
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
        data = await final_result(next_event)
    finally:
        release()
    message = {"role": "assistant", "content": data["content"], "reasoning_content": data["reasoning_content"]}
    if data["tool_calls"]:
        message["tool_calls"] = data["tool_calls"]
    return {"id": identifier, "object": "chat.completion", "created": created, "model": body.model,
            "choices": [{"index": 0, "message": message, "finish_reason": data["finish_reason"]}],
            "usage": data["usage"], "metrics": data["metrics"]}


class CleanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    model_size: Literal["1B", "4B", "7B"] | None = None
    # Only a JSON boolean: a malformed value must never silently disable the guard.
    guard: StrictBool = True


def clean_failure(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


@app.post("/v1/clean")
async def clean(body: CleanRequest, request: Request):
    """Transcript cleanup for one turn: the only text allowed to reach the agent.

    Incomplete, empty or (with the guard) uncertain corrections are refused;
    the raw transcript is never forwarded as a fallback.
    """
    size = body.model_size or (next(iter(worker.models)) if len(worker.models) == 1 else None)
    entry = worker._model_health.get(size)
    completion = CompletionRequest(model="k2-cleaner", model_size=body.model_size, messages=cleaner_messages(body.text),
                                   temperature=0, max_tokens=2048, reasoning_effort=role_effort(entry, "k2-cleaner") if entry else "low")
    started = time.perf_counter()
    try:
        next_event, release = await begin_generation(completion, request)
        try:
            data = await final_result(next_event)
        finally:
            release()
    except HTTPException as exc:
        if exc.status_code == 422:
            raise clean_failure(422, "cleaner_incomplete", "Le correcteur n’a pas terminé le nettoyage. La demande n’a pas été exécutée.") from exc
        raise clean_failure(exc.status_code, "cleaner_unavailable", "Le nettoyage a échoué. Vérifiez l’état des modèles locaux.") from exc
    text = data["content"].strip()
    if data["finish_reason"] != "stop":
        raise clean_failure(422, "cleaner_incomplete", "Le correcteur n’a pas terminé le nettoyage. La demande n’a pas été exécutée.")
    if not text:
        raise clean_failure(422, "cleaner_empty", "Le correcteur n’a renvoyé aucun texte corrigé. La demande n’a pas été exécutée.")
    if body.guard and cleanup_problem(body.text, text) is not None:
        raise clean_failure(422, "cleaner_uncertain", "Correction incertaine. Reformulez votre demande ; aucune action n’a été lancée.")
    return {"text": text, "elapsed_ms": round((time.perf_counter() - started) * 1000)}


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
