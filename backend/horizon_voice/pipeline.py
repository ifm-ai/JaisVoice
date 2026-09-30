from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .clients import InferenceClients, PipelineError, validate_asr_language, validate_model_size, validate_cleanup_guard

logger = logging.getLogger(__name__)
EventSink = Callable[[dict[str, Any]], Awaitable[None]]
ReplyRunner = Callable[["Turn", str], Awaitable[str]]


@dataclass
class Turn:
    id: str
    started: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    actions: int = 0
    response: str = ""
    clarification_question: str | None = None
    failure: PipelineError | None = None
    timings: dict[str, int] = field(default_factory=dict)
    agent_started: float | None = None
    language: str | None = None
    cleaner_model: str | None = None
    agent_model: str | None = None
    cleanup_guard_enabled: bool = True
    conversation_id: str | None = None


class TurnCoordinator:
    """One active request; no old event or tool request can enter a newer turn."""

    def __init__(self, clients: InferenceClients, emit: EventSink, reply: ReplyRunner):
        self.clients = clients
        self._emit = emit
        self.reply = reply
        self.active: Turn | None = None
        self._task: asyncio.Task[None] | None = None
        self._transition = asyncio.Lock()

    def is_current(self, turn: Turn) -> bool:
        return self.active is turn and not turn.cancelled

    async def event(self, turn: Turn, kind: str, **fields: Any) -> None:
        if self.is_current(turn):
            await self._emit({"type": kind, "turn_id": turn.id, **fields})

    async def start(self, turn_id: str, audio: bytes, *, language: str | None = None,
                    cleaner_model: str | None = None, agent_model: str | None = None, cleanup_guard_enabled: bool | str | None = None, conversation_id: str | None = None) -> asyncio.Task[None]:
        language = validate_asr_language(language)
        cleanup_guard_enabled = validate_cleanup_guard(cleanup_guard_enabled)
        cleaner_model, agent_model = validate_model_size(cleaner_model), validate_model_size(agent_model)
        async with self._transition:
            await self._cancel_locked(None)
            turn = Turn(turn_id, language=language, cleaner_model=cleaner_model, agent_model=agent_model, cleanup_guard_enabled=cleanup_guard_enabled, conversation_id=conversation_id)
            self.active = turn
            self._task = asyncio.create_task(self._run(turn, audio), name=f"turn:{turn_id}")
            return self._task

    async def start_text(self, turn_id: str, text: str, *, cleaner_model: str | None = None,
                         agent_model: str | None = None, cleanup_guard_enabled: bool | str | None = None, conversation_id: str | None = None) -> asyncio.Task[None]:
        """Explicit diagnostic input; bypass only ASR, not cleanup or permissions."""
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 4000:
            raise PipelineError("text_invalid", "Le texte de test est vide ou trop long.")
        cleanup_guard_enabled = validate_cleanup_guard(cleanup_guard_enabled)
        cleaner_model, agent_model = validate_model_size(cleaner_model), validate_model_size(agent_model)
        async with self._transition:
            await self._cancel_locked(None)
            turn = Turn(turn_id, cleaner_model=cleaner_model, agent_model=agent_model, cleanup_guard_enabled=cleanup_guard_enabled, conversation_id=conversation_id)
            self.active = turn
            self._task = asyncio.create_task(self._run(turn, None, text=text), name=f"text-turn:{turn_id}")
            return self._task

    async def cancel(self, turn_id: str | None = None) -> None:
        async with self._transition:
            await self._cancel_locked(turn_id)

    async def _cancel_locked(self, turn_id: str | None) -> None:
        turn, task = self.active, self._task
        if turn is None or (turn_id is not None and turn.id != turn_id):
            return
        if task is not None and not task.done():
            turn.cancelled = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self._emit({"type": "cancelled", "turn_id": turn.id})
        self.active = None
        self._task = None

    async def _run(self, turn: Turn, audio: bytes | None, *, text: str | None = None) -> None:
        try:
            if text is None:
                await self.event(turn, "status", stage="transcribing")
                stage_started = time.monotonic()
                raw = await self.clients.transcribe(audio, language=turn.language)
                turn.timings["asr_ms"] = round((time.monotonic() - stage_started) * 1000)
                await self.event(turn, "transcript", text=raw, elapsed_ms=turn.timings["asr_ms"])
            else:
                raw = text
                await self.event(turn, "transcript", text=raw)
            await self.event(turn, "status", stage="refining")
            stage_started = time.monotonic()
            clean = await self.clients.clean(raw, **({"model_size": turn.cleaner_model} if turn.cleaner_model else {}),
                **({"cleanup_guard_enabled": False} if not turn.cleanup_guard_enabled else {}))
            turn.timings["cleanup_ms"] = round((time.monotonic() - stage_started) * 1000)
            await self.event(turn, "correction", text=clean, elapsed_ms=turn.timings["cleanup_ms"])
            if not self.is_current(turn):
                return
            await self.event(turn, "status", stage="acting")
            turn.agent_started = time.monotonic()
            answer = await self.reply(turn, clean)
            turn.timings["agent_ms"] = round((time.monotonic() - turn.agent_started) * 1000)
            if turn.failure:
                raise turn.failure
            await self.event(turn, "clarification_request" if turn.clarification_question else "complete", text=turn.clarification_question or answer, elapsed_ms=round((time.monotonic() - turn.started) * 1000), metrics=turn.timings)
        except asyncio.CancelledError:
            raise
        except PipelineError as exc:
            await self.event(turn, "error", text=str(exc), error_code=exc.code)
        except Exception:
            logger.exception("Turn failed: %s", turn.id)
            await self.event(turn, "error", text="Le traitement a échoué. Réessayez depuis la barre des menus.", error_code="processing_failed")
