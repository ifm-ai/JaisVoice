"""Local MLX gateway: one resident Jais 3 model, shared by transcript cleanup and the agent.

`/v1/clean` cleans one transcript; `/v1/chat/completions` (streamed, OpenAI
style) serves the Pi agent. A single executor thread runs every load and
generation, so Metal work never races. A disconnected client cancels its
queued work, or stops its generation at the next token.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import gc
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import model_store
from .prompts import cleaner_messages
from .protocol import JaisParser, normalize_messages

logger = logging.getLogger("jais.mlx")
Size = Literal["0.9B", "3.7B", "7B"]
EFFORTS = ("low", "medium", "high")
# Token budgets, including the reserved output.
CLEANER_CONTEXT, AGENT_CONTEXT = 4096, 32768
# The agent's sampling, as the Jais 3 model cards recommend for thinking mode
# (temperature 0.7 is their Arabic value; 1.0 for English loosens tool calls).
# Cleanup stays greedy: it must reproduce the transcript, not vary it.
AGENT_TEMPERATURE, AGENT_TOP_P, AGENT_TOP_K = 0.7, 0.95, 20
# A request may ask for more output than fits; it gets what the context leaves.
MIN_OUTPUT_TOKENS = 256


def prepare_prompt(tokenizer, messages, tools, effort: str):
    """Render the native prompt, honoring a reasoning block the template already closes.

    Never infer a missing reasoning delimiter from generated content.
    """
    rendered = tokenizer.apply_chat_template(messages, tools=tools or None, tokenize=False,
        add_generation_prompt=True, reasoning_effort=effort, tool_call_format="xml", tool_presentation_format="markdown")
    opening = {"low": "ifm|think_faster", "medium": "ifm|think_fast", "high": "ifm|think"}[effort]
    suffix = rendered.rsplit("<|ifm|im_start|>assistant\n", 1)
    pattern = rf"<{re.escape(opening)}>\s*(?P<closed></{re.escape(opening)}>)?\s*"
    match = re.fullmatch(pattern, suffix[-1]) if len(suffix) == 2 else None
    if match is None:
        raise ValueError("Unsupported generation prefix; refusing to guess reasoning framing")
    parser = JaisParser(tools, effort)
    if match.group("closed"):
        parser.mode = "content"
    return tokenizer.encode(rendered, add_special_tokens=False), parser


def output_budget(prompt_tokens: int, requested: int, context: int) -> int:
    """The output tokens a request gets: what it asked for, within what the context leaves."""
    available = context - prompt_tokens
    if available < min(requested, MIN_OUTPUT_TOKENS):
        raise ValueError(f"Context budget exceeded: {prompt_tokens} input tokens leave {available} of {context} for the answer")
    return min(requested, available)


@dataclass
class Job:
    size: str
    messages: list
    tools: list
    effort: str
    max_tokens: int
    temperature: float
    top_p: float
    top_k: int
    context: int


class Worker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jais-mlx")
        self.lock = threading.Lock()
        self.model = None  # (size, model, tokenizer) of the resident model
        self.loading: str | None = None
        self.errors: dict[str, str] = {}
        self.status, self.message = "starting", "Loading models…"
        self.changing = False  # a load or unload is in progress
        self.pending = 0
        self.cancel_flags: set[threading.Event] = set()

    @property
    def loaded_size(self) -> str | None:
        return self.model[0] if self.model else None

    def health(self) -> dict:
        # Read the catalog and disk again: downloads and deletions happen outside this process.
        try:
            entries = model_store.language_models()
        except (OSError, ValueError, KeyError) as exc:
            return {"status": "error", "message": f"Invalid model catalog: {exc}", "models": {}}
        base = model_store.models_dir()
        with self.lock:
            models = {}
            for size, entry in entries.items():
                installed = model_store.is_installed(entry, base)
                status = ("ready" if size == self.loaded_size else "loading" if size == self.loading
                          else "error" if size in self.errors else "unloaded" if installed else "not_installed")
                models[size] = {"display_name": entry["display_name"], "installed": installed, "status": status}
                if status == "error":
                    models[size]["error"] = self.errors[size]
            return {"status": self.status, "message": self.message, "models": models}

    def start(self):
        """Load the size the app asked for when installed, else the first installed one."""
        try:
            base = model_store.models_dir()
            installed = [size for size, entry in model_store.language_models().items() if model_store.is_installed(entry, base)]
            preferred = os.environ.get("JAIS_MODEL_SIZE", "3.7B")
            size = preferred if preferred in installed else (installed[0] if installed else None)
            if size:
                self.change(size, "load")
            with self.lock:
                self.status, self.message = "ready", "Ready" if size else "No language model installed"
        except Exception as exc:
            logger.exception("MLX model loading failed")
            with self.lock:
                self.status, self.message = "error", str(exc)

    def change(self, size: str, action: str):
        """Load or unload one size; only one model stays resident."""
        import mlx.core as mx
        entry = model_store.language_models().get(size)
        if entry is None:
            raise ValueError(f"The {size} model is not in the catalog.")
        self.errors.pop(size, None)
        if action == "load" and not model_store.is_installed(entry, model_store.models_dir()):
            raise ValueError(f"{entry['display_name']} is not installed: download it in Settings.")
        if self.model and (self.loaded_size == size) == (action == "unload"):
            with self.lock:
                self.model = None
            gc.collect()
            mx.clear_cache()
        if action == "unload" or self.model:
            return
        with self.lock:
            self.loading = size
        try:
            from .model import load_model
            model, tokenizer, _ = load_model(model_store.model_path(entry, model_store.models_dir()))
            for effort in EFFORTS:
                prepare_prompt(tokenizer, [{"role": "user", "content": "Ready?"}], [], effort)
            mx.eval(model(mx.array([[0, 1]])))
            with self.lock:
                self.model = (size, model, tokenizer)
            logger.info("Loaded %s; active memory %.2f GB", size, mx.get_active_memory() / 1e9)
        except Exception as exc:
            self.errors[size] = str(exc)
            raise
        finally:
            with self.lock:
                self.loading = None

    def generate(self, job: Job, canceled: threading.Event, emit):
        try:
            if canceled.is_set():
                return
            from mlx_lm import stream_generate
            from mlx_lm.sample_utils import make_sampler
            if self.loaded_size != job.size:
                raise ValueError(f"The {job.size} model is not loaded: load it in Settings.")
            _, model, tokenizer = self.model
            tokens, parser = prepare_prompt(tokenizer, job.messages, job.tools, job.effort)
            max_tokens = output_budget(len(tokens), job.max_tokens, job.context)
            emit(("ready", None))
            def progress(done, total):
                if canceled.is_set():
                    raise InterruptedError("Request cancelled during prompt processing")
            generator = stream_generate(model, tokenizer, tokens, max_tokens=max_tokens,
                sampler=make_sampler(temp=job.temperature, top_p=job.top_p, top_k=job.top_k),
                prefill_step_size=256, prompt_progress_callback=progress)
            last = None
            try:
                for chunk in generator:
                    if canceled.is_set():
                        return
                    last = chunk
                    for delta in parser.feed(chunk.text):
                        if "tool_calls" not in delta:
                            emit(("delta", delta))
            finally:
                generator.close()
            finish = parser.finish(last.finish_reason if last else "length")
            # Tool calls are released only after a complete, valid generation; one per chunk.
            for call in parser.calls:
                emit(("delta", {"tool_calls": [call]}))
            emit(("done", {"finish_reason": finish, "content": parser.content,
                           "usage": {"prompt_tokens": len(tokens), "completion_tokens": last.generation_tokens,
                                     "total_tokens": len(tokens) + last.generation_tokens}}))
        except InterruptedError:
            pass
        except Exception as exc:
            logger.warning("Generation failed: %s", exc)
            emit(("error", str(exc)))
        finally:
            emit(("end", None))


worker = Worker()


@asynccontextmanager
async def lifespan(app):
    asyncio.get_running_loop().run_in_executor(worker.executor, worker.start)
    yield
    for flag in list(worker.cancel_flags):
        flag.set()
    worker.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Jais Voice local gateway", lifespan=lifespan)


@app.get("/health")
async def health():
    return worker.health()


class MemoryRequest(BaseModel):
    action: Literal["load", "unload"]


@app.post("/models/{size}/memory")
async def model_memory(size: Size, body: MemoryRequest):
    # Reserve before yielding so concurrent HTTP requests cannot race inference.
    if worker.status == "starting" or worker.changing or worker.pending:
        raise HTTPException(409, "An operation is in progress. Try again when it ends.")
    worker.changing = True
    async def perform():
        try:
            await asyncio.get_running_loop().run_in_executor(worker.executor, worker.change, size, body.action)
            with worker.lock:
                worker.status, worker.message = "ready", "Ready"
        finally:
            worker.changing = False
    # A cancelled HTTP client must not release the reservation before Metal finishes.
    task = asyncio.create_task(perform())
    try:
        await asyncio.shield(task)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return worker.health()


async def begin(job: Job, request: Request):
    """Queue one generation; returns its event reader and the release callback."""
    if worker.changing:
        raise HTTPException(409, "Model memory is being changed.")
    if worker.status != "ready":
        raise HTTPException(503, worker.message)
    if worker.loaded_size != job.size:
        raise HTTPException(409, f"The {job.size} model is not loaded: load it in Settings.")
    if worker.pending >= 8:
        raise HTTPException(429, "Local generation queue is full")
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    canceled = threading.Event()
    worker.cancel_flags.add(canceled)
    worker.pending += 1
    def emit(event):
        if not canceled.is_set():
            loop.call_soon_threadsafe(queue.put_nowait, event)
    loop.run_in_executor(worker.executor, worker.generate, job, canceled, emit)
    def release():
        if canceled in worker.cancel_flags:
            canceled.set()
            worker.cancel_flags.discard(canceled)
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
        kind, data = await next_event()
        if kind != "ready":
            raise HTTPException(400, data or "Cancelled")
    except BaseException:
        release()
        raise
    return next_event, release


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str
    messages: list[dict] = Field(min_length=1, max_length=256)
    tools: list[dict] | None = None
    max_tokens: int = Field(default=8192, ge=1, le=AGENT_CONTEXT)
    temperature: float = Field(default=AGENT_TEMPERATURE, ge=0, le=2)
    top_p: float = Field(default=AGENT_TOP_P, gt=0, le=1)
    top_k: int = Field(default=AGENT_TOP_K, ge=0, le=1000)
    reasoning_effort: Literal["low", "medium", "high"] = "low"


@app.post("/v1/chat/completions")
async def completions(body: CompletionRequest, request: Request):
    """The agent's streamed completions; the model ID (`jais-3-3.7b`…) selects the size."""
    size = next((size for size, entry in model_store.language_models().items() if entry["id"] == body.model), None)
    if size is None:
        raise HTTPException(404, "Unknown model")
    try:
        messages = normalize_messages(body.messages)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    job = Job(size, messages, body.tools or [], body.reasoning_effort, body.max_tokens, body.temperature, body.top_p, body.top_k, AGENT_CONTEXT)
    next_event, release = await begin(job, request)
    identifier, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())
    def chunk(delta, finish=None):
        return {"id": identifier, "object": "chat.completion.chunk", "created": created,
                "model": body.model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    async def events():
        try:
            yield "data: " + json.dumps(chunk({"role": "assistant"})) + "\n\n"
            while True:
                kind, data = await next_event()
                if kind == "delta":
                    yield "data: " + json.dumps(chunk(data), ensure_ascii=False) + "\n\n"
                elif kind == "done":
                    yield "data: " + json.dumps(chunk({}, data["finish_reason"]) | {"usage": data["usage"]}) + "\n\n"
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


class CleanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    model_size: Size | None = None


def clean_failure(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


@app.post("/v1/clean")
async def clean(body: CleanRequest, request: Request):
    """Transcript cleanup for one turn: the only text allowed to reach the agent.

    Incomplete or empty corrections are refused; the raw transcript is never
    forwarded as a fallback.
    """
    job = Job(body.model_size or worker.loaded_size or "", cleaner_messages(body.text), [], "low", 2048, 0.0, 0.95, 0, CLEANER_CONTEXT)
    started = time.perf_counter()
    incomplete = clean_failure(422, "cleaner_incomplete", "The corrector did not finish cleanup. The request was not executed.")
    try:
        next_event, release = await begin(job, request)
    except HTTPException as exc:
        raise clean_failure(exc.status_code, "cleaner_unavailable", "Cleanup failed. Check the local model status.") from exc
    try:
        while True:
            kind, data = await next_event()
            if kind == "done":
                break
            if kind in ("error", "end"):
                raise incomplete
    finally:
        release()
    text = data["content"].strip()
    if data["finish_reason"] != "stop":
        raise incomplete
    if not text:
        raise clean_failure(422, "cleaner_empty", "The corrector returned no corrected text. The request was not executed.")
    return {"text": text, "elapsed_ms": round((time.perf_counter() - started) * 1000)}


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
