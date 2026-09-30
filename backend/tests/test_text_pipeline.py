"""Explicit text diagnostics still use the cleanup safety boundary."""
import pytest

from horizon_voice.clients import PipelineError
from horizon_voice.pipeline import TurnCoordinator


async def test_text_diagnostic_skips_only_asr():
    calls, events = [], []

    class Clients:
        async def transcribe(self, *_args, **_kwargs):
            pytest.fail("Text diagnostic must not imply an ASR test")

        async def clean(self, text):
            calls.append(("clean", text))
            return "corrected text"

    async def reply(turn, text):
        calls.append(("agent", text))
        return "done"

    async def emit(event):
        events.append(event)

    coordinator = TurnCoordinator(Clients(), emit, reply)
    await (await coordinator.start_text("test", "explicit raw text"))
    assert calls == [("clean", "explicit raw text"), ("agent", "corrected text")]
    assert [event["type"] for event in events] == ["transcript", "status", "correction", "status", "complete"]
    assert "asr_ms" not in events[-1]["metrics"]


async def test_text_cleanup_failure_never_reaches_agent():
    events = []

    class Clients:
        async def clean(self, text):
            raise PipelineError("cleaner_uncertain", "unsafe cleanup")

    async def reply(*args):
        pytest.fail("No tools may run after rejected cleanup")

    async def emit(event):
        events.append(event)

    coordinator = TurnCoordinator(Clients(), emit, reply)
    await (await coordinator.start_text("test", "input"))
    assert events[-1]["error_code"] == "cleaner_uncertain"


@pytest.mark.parametrize("text", ["", "   ", "a" * 4001, "ع" * 2001, None])
async def test_text_diagnostic_rejects_unbounded_input(text):
    coordinator = TurnCoordinator(None, None, None)
    with pytest.raises(PipelineError):
        await coordinator.start_text("test", text)
    assert coordinator.active is None


async def test_clarification_is_terminal_waiting_event_then_answer_continues():
    events, prompts = [], []
    class Clients:
        async def clean(self, text): return text
    async def emit(event): events.append(event)
    async def reply(turn, text):
        prompts.append((turn.conversation_id, text))
        if text == "Find a restaurant":
            turn.clarification_question = "Which city?"
            return "Which city?"
        return "Searching in Paris."
    coordinator = TurnCoordinator(Clients(), emit, reply)
    await (await coordinator.start_text("question", "Find a restaurant", conversation_id="conversation"))
    assert events[-1]["type"] == "clarification_request"
    assert events[-1]["text"] == "Which city?"
    assert not any(event["type"] == "complete" for event in events)
    await (await coordinator.start_text("answer", "Paris", conversation_id="conversation"))
    assert events[-1]["type"] == "complete"
    assert prompts == [("conversation", "Find a restaurant"), ("conversation", "Paris")]
