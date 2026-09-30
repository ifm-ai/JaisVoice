"""Actual LiveKit AgentSession; inference is an explicitly named test double."""
import asyncio
import json
import uuid
from collections import deque
from types import SimpleNamespace

import pytest
from livekit.agents import AgentSession, RunContext, function_tool, llm

from horizon_voice.agent import HorizonAgent, RoomPipeline
from horizon_voice.browser import BrowserServer
from horizon_voice.config import Settings
from horizon_voice.pipeline import Turn, TurnCoordinator


class ExplicitTestLLM(llm.LLM):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
        self.inputs.append(chat_ctx.copy())
        return ExplicitTestStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class ExplicitTestStream(llm.LLMStream):
    async def _run(self):
        self._event_ch.send_nowait(llm.ChatChunk(id="test-only", delta=llm.ChoiceDelta(content="Bonjour", extra={"reasoning_content": "private test thought"})))
        await asyncio.sleep(0)
        self._event_ch.send_nowait(llm.ChatChunk(id="test-only", delta=llm.ChoiceDelta(content=" à Paris.")))


async def test_real_livekit_session_preserves_turn_context_and_hides_reasoning():
    events = []
    async def emit(event): events.append(event)
    class ExplicitTestClients:
        async def transcribe(self, data, *, language=None): return "Abou Dhabi non Paris"
        async def clean(self, data): return "Paris"

    pipeline = RoomPipeline.__new__(RoomPipeline)
    model = ExplicitTestLLM()
    pipeline.session = AgentSession(llm=model, user_away_timeout=None)
    pipeline.session.output.set_audio_enabled(False)
    pipeline.speech_turns = {}
    pipeline.coordinator = TurnCoordinator(ExplicitTestClients(), emit, pipeline.reply)
    await pipeline.session.start(agent=HorizonAgent(pipeline.coordinator))
    try:
        task = await pipeline.coordinator.start("actual-livekit-test", b"explicit-test-data")
        await asyncio.wait_for(task, 5)
        assert events[-1]["type"] == "complete", events
        assert events[-1]["text"] == "Bonjour à Paris."
        assert [e["text"] for e in events if e["type"] == "response_delta"] == ["Bonjour", " à Paris."]
        deltas = [event for event in events if event["type"] == "response_delta"]
        assert deltas[0]["elapsed_ms"] >= 0
        assert "elapsed_ms" not in deltas[1]
        assert events[-1]["metrics"]["first_response_ms"] == deltas[0]["elapsed_ms"]
        assert all("private test thought" not in str(event) for event in events)
        user_messages = [message.text_content for message in model.inputs[0].items if getattr(message, "role", "") == "user"]
        assert user_messages == ["Paris"]
    finally:
        await pipeline.session.aclose()


async def test_real_livekit_session_does_not_complete_after_partial_generation_failure():
    """Provider errors after visible text must still end the turn as an error."""
    class PartialFailureLLM(ExplicitTestLLM):
        def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
            return PartialFailureStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)

    class PartialFailureStream(llm.LLMStream):
        async def _run(self):
            self._event_ch.send_nowait(llm.ChatChunk(id="partial-failure", delta=llm.ChoiceDelta(content="Début de réponse")))
            await asyncio.sleep(0)
            raise ValueError("Explicit incomplete-generation test double")

    class ExplicitTestClients:
        async def transcribe(self, data, *, language=None): return "Le texte original"
        async def clean(self, data): return "Le texte corrigé"

    events = []
    async def emit(event): events.append(event)
    pipeline = RoomPipeline.__new__(RoomPipeline)
    pipeline.session = AgentSession(llm=PartialFailureLLM(), user_away_timeout=None)
    pipeline.session.output.set_audio_enabled(False)
    pipeline.speech_turns = {}
    pipeline.coordinator = TurnCoordinator(ExplicitTestClients(), emit, pipeline.reply)
    await pipeline.session.start(agent=HorizonAgent(pipeline.coordinator))
    try:
        task = await pipeline.coordinator.start("actual-livekit-failure-test", b"explicit-test-data")
        await asyncio.wait_for(task, 5)
        assert events[-1]["type"] == "error", events
        assert not any(event["type"] == "complete" for event in events)
        assert pipeline.coordinator.active.failure is not None
    finally:
        await pipeline.session.aclose()


async def test_browser_wrapper_blocks_stale_and_fifty_first_calls():
    executed, events = [], []
    async def emit(event): events.append(event)
    async def unused(*args): pass
    coordinator = TurnCoordinator(None, emit, unused)
    turn = Turn("test-turn")
    coordinator.active = turn
    server = BrowserServer.__new__(BrowserServer)
    server.coordinator = coordinator
    server.speech_turn = lambda speech_id: turn if speech_id == "speech" else None
    server.max_actions = 50
    server._action_lock = asyncio.Lock()
    server._calls = {}

    @function_tool(raw_schema={"name": "browser_snapshot", "parameters": {"type": "object", "properties": {}}})
    async def explicit_browser_double(raw_arguments: dict):
        executed.append(raw_arguments)
        return "Actual test double result"

    wrapped = server._wrap(explicit_browser_double)
    ctx = SimpleNamespace(speech_handle=SimpleNamespace(id="speech", interrupted=False), function_call=SimpleNamespace(call_id="call"))
    for _ in range(50):
        await wrapped(ctx, {})
    with pytest.raises(llm.ToolError, match="50-action"):
        await wrapped(ctx, {})
    assert len(executed) == 50
    assert [event["type"] for event in events] == ["tool_start", "tool_end"] * 50
    turn.cancelled = True
    with pytest.raises(llm.ToolError, match="cancelled"):
        await wrapped(ctx, {})
    assert len(executed) == 50


async def test_cancel_before_audio_header_prevents_delayed_upload():
    pipeline = RoomPipeline.__new__(RoomPipeline)
    pipeline.client_identity = "test-client"
    pipeline.incoming_turn = None
    pipeline.cancelled_ids = deque(maxlen=256)
    pipeline.received_ids = deque(maxlen=256)
    pipeline.tasks = set()
    async def unused(*args): pass
    pipeline.coordinator = TurnCoordinator(None, unused, unused)
    turn_id = str(uuid.uuid4())
    packet = SimpleNamespace(topic="horizon.control", participant=SimpleNamespace(identity="test-client"), data=json.dumps({"type": "cancel", "turn_id": turn_id}).encode())
    pipeline.on_data(packet)
    closed = []
    reader = SimpleNamespace(info=SimpleNamespace(attributes={"turn_id": turn_id}), close=lambda: closed.append(True))
    pipeline.on_audio(reader, "test-client")
    await asyncio.gather(*list(pipeline.tasks))
    assert closed == [True]
    assert pipeline.incoming_turn is None
    assert pipeline.coordinator.active is None


@pytest.mark.parametrize("language,expected", [(None, None), ("", None), ("fr", "fr"), ("ar", "ar")])
async def test_audio_stream_language_snapshot_is_forwarded(language, expected):
    pipeline, events, started, reader = make_audio_pipeline(language)
    pipeline.on_audio(reader, "test-client")
    # The receive coroutine must keep the header's choice, not consult mutable
    # preferences or another incoming stream after its first await.
    reader.info.attributes["language"] = "it"
    await asyncio.gather(*list(pipeline.tasks))
    assert started == [(reader.info.attributes["turn_id"], b"explicit-test-audio", expected)]
    assert events == []
    assert reader.closed


@pytest.mark.parametrize("language", ["auto", "de", "FR", "fr\r\ninjected"])
async def test_bad_audio_stream_language_is_closed_without_reading_or_starting_asr(language):
    pipeline, events, started, reader = make_audio_pipeline(language)
    pipeline.on_audio(reader, "test-client")
    await asyncio.gather(*list(pipeline.tasks))
    assert started == []
    assert not reader.read
    assert reader.closed
    assert events[0]["error_code"] == "asr_language"
    assert events[0]["turn_id"] == reader.info.attributes["turn_id"]


async def test_oversize_audio_stream_is_rejected_before_asr():
    pipeline, events, started, reader = make_audio_pipeline(None)
    reader.content = b"x" * (Settings().max_audio_bytes + 1)
    pipeline.on_audio(reader, "test-client")
    await asyncio.gather(*list(pipeline.tasks))
    assert started == []
    assert reader.closed
    assert events[0]["error_code"] == "audio_too_long"
    assert "30 secondes" in events[0]["text"]


def make_audio_pipeline(language):
    """Transport-only test double: no ASR, model, browser, or network calls."""
    events, started = [], []
    pipeline = RoomPipeline.__new__(RoomPipeline)
    pipeline.client_identity = "test-client"
    pipeline.incoming_turn = None
    pipeline.cancelled_ids = deque(maxlen=256)
    pipeline.received_ids = deque(maxlen=256)
    pipeline.tasks = set()
    pipeline.settings = Settings()
    pipeline.ready = asyncio.Event()
    pipeline.ready.set()
    pipeline.startup_error = None
    async def cancel(): pass
    async def start(turn_id, content, *, language=None):
        started.append((turn_id, content, language))
    async def emit(event): events.append(event)
    pipeline.coordinator = SimpleNamespace(cancel=cancel, start=start)
    pipeline.emit = emit

    class ExplicitAudioReader:
        def __init__(self):
            attributes = {"turn_id": str(uuid.uuid4())}
            if language is not None:
                attributes["language"] = language
            self.info = SimpleNamespace(attributes=attributes)
            self.content = b"explicit-test-audio"
            self.closed = False
            self.read = False
        async def __aiter__(self):
            self.read = True
            yield self.content
        def close(self): self.closed = True
    return pipeline, events, started, ExplicitAudioReader()

async def test_followups_clean_each_recording_preserve_history_and_reset_for_new_conversation():
    events, cleaned_inputs = [], []
    async def emit(event): events.append(event)
    class Clients:
        async def transcribe(self, audio, *, language=None): return audio.decode()
        async def clean(self, raw):
            cleaned_inputs.append(raw)
            return raw.replace('um ', '')

    pipeline = RoomPipeline.__new__(RoomPipeline)
    model = ExplicitTestLLM()
    pipeline.session = AgentSession(llm=model, user_away_timeout=None)
    pipeline.session.output.set_audio_enabled(False)
    pipeline.speech_turns = {}
    pipeline.coordinator = TurnCoordinator(Clients(), emit, pipeline.reply)
    await pipeline.session.start(agent=HorizonAgent(pipeline.coordinator))
    try:
        for index, (text, conversation) in enumerate([
            ('um Remember Paris', 'conversation-a'),
            ('um What city?', 'conversation-a'),
            ('um Repeat the first request', 'conversation-a'),
            ('um New question', 'conversation-b'),
        ]):
            task = await pipeline.coordinator.start(str(index), text.encode(), conversation_id=conversation)
            await asyncio.wait_for(task, 5)
            assert events[-1]['type'] == 'complete', events
        users = lambda context: [i.text_content for i in context.items if getattr(i, 'role', '') == 'user']
        assert users(model.inputs[1]) == ['Remember Paris', 'What city?']
        assert users(model.inputs[2]) == ['Remember Paris', 'What city?', 'Repeat the first request']
        assert any(getattr(i, 'role', '') == 'assistant' and i.text_content == 'Bonjour à Paris.' for i in model.inputs[1].items)
        assert users(model.inputs[3]) == ['New question']
        assert any(getattr(i, 'role', '') == 'system' for i in model.inputs[3].items)
        assert len(cleaned_inputs) == 4
        assert [e['text'] for e in events if e['type'] == 'correction'] == ['Remember Paris', 'What city?', 'Repeat the first request', 'New question']
    finally:
        await pipeline.session.aclose()

async def test_audio_conversation_id_reaches_coordinator():
    pipeline, events, started, reader = make_audio_pipeline(None)
    conversation = str(uuid.uuid4())
    reader.info.attributes['conversation_id'] = conversation
    async def start(turn_id, content, **kwargs): started.append(kwargs)
    pipeline.coordinator.start = start
    pipeline.on_audio(reader, 'test-client')
    await asyncio.gather(*list(pipeline.tasks))
    assert started == [{'language': None, 'conversation_id': conversation}]
    assert events == []


async def test_real_session_clarification_stops_and_dictated_answer_keeps_question():
    from horizon_voice.desktop import DesktopBridge

    class ClarificationLLM(ExplicitTestLLM):
        def chat(self, *, chat_ctx, tools=None, conn_options, **kwargs):
            self.inputs.append(chat_ctx.copy())
            return ClarificationStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)

    class ClarificationStream(llm.LLMStream):
        async def _run(self):
            if len(self._llm.inputs) == 1:
                delta = llm.ChoiceDelta(tool_calls=[llm.FunctionToolCall(name="ask_clarifying_question", arguments='{"question":"Which city?"}', call_id="clarify-1")])
            else:
                delta = llm.ChoiceDelta(content="Searching in Paris.")
            self._event_ch.send_nowait(llm.ChatChunk(id="clarification-test", delta=delta))

    class Clients:
        async def transcribe(self, data, *, language=None): return data.decode()
        async def clean(self, text): return text

    events = []
    async def emit(event): events.append(event)
    pipeline = RoomPipeline.__new__(RoomPipeline)
    model = ClarificationLLM()
    pipeline.session = AgentSession(llm=model, user_away_timeout=None)
    pipeline.session.output.set_audio_enabled(False)
    pipeline.speech_turns = {}
    pipeline.coordinator = TurnCoordinator(Clients(), emit, pipeline.reply)
    pipeline.desktop = DesktopBridge(pipeline.coordinator, pipeline.speech_turns.get, emit)
    await pipeline.session.start(agent=HorizonAgent(pipeline.coordinator, desktop=pipeline.desktop))
    try:
        await asyncio.wait_for(await pipeline.coordinator.start("question", b"Find a restaurant", conversation_id="same"), 5)
        assert events[-1]["type"] == "clarification_request", events
        assert events[-1]["text"] == "Which city?"
        assert len(model.inputs) == 1  # No extra inference while awaiting dictation.
        await asyncio.wait_for(await pipeline.coordinator.start("answer", b"Paris", conversation_id="same"), 5)
        assert events[-1]["type"] == "complete", events
        assert events[-1]["text"] == "Searching in Paris."
        context = model.inputs[-1]
        assert [i.text_content for i in context.items if i.type == "message" and i.role == "user"] == ["Find a restaurant", "Paris"]
        assert any(i.type == "function_call" and "Which city?" in i.arguments for i in context.items)
    finally:
        await pipeline.session.aclose()
