import asyncio
import base64
import json
import io
import wave

import httpx
import pytest

from horizon_voice.clients import InferenceClients, PipelineError, validate_asr_language, validate_wav
from horizon_voice.config import MAX_AUDIO_SECONDS, Settings
from horizon_voice.pipeline import Turn, TurnCoordinator


def wav(frames: int = 3200) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(16000)
        sound.writeframes(b"\0\0" * frames)
    return output.getvalue()


@pytest.mark.parametrize("payload,code", [
    ({"text": "مرحبا", "refused": True}, "asr_refused"),
    ({"text": "مرحبا", "truncated": True}, "asr_truncated"),
    ({"text": "  "}, "asr_empty"),
    ([], "asr_invalid"),
])
async def test_asr_rejects_invalid_or_partial_output(payload, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as http:
        with pytest.raises(PipelineError) as error:
            await InferenceClients(Settings(), http).transcribe(wav())
    assert error.value.code == code


async def test_local_asr_audio_and_raw_arabic():
    def respond(request):
        assert request.url.host == "127.0.0.1" and request.url.port == 8790
        assert base64.b64decode(json.loads(request.content)["audio"]) == wav()
        return httpx.Response(200, json={"text": " إلى أحمد، لا، إلى سارة "})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await InferenceClients(Settings(), http).transcribe(wav()) == "إلى أحمد، لا، إلى سارة"


@pytest.mark.parametrize("language", [None, "", "fr", "ar", "en", "es", "it", "msa"])
async def test_asr_language_is_sent_only_when_explicit(language):
    def respond(request):
        body = json.loads(request.content)
        assert base64.b64decode(body["audio"]) == wav()
        assert body.get("language") == (language or None)
        return httpx.Response(200, json={"text": "Bonjour"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await InferenceClients(Settings(), http).transcribe(wav(), language=language) == "Bonjour"


@pytest.mark.parametrize("language", ["auto", "de", "FR", " fr ", "fr\r\ninjected", 1, [], {}])
async def test_invalid_asr_language_never_reaches_network(language):
    def forbidden(request):
        pytest.fail("Invalid language must be rejected before sending audio")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(PipelineError) as error:
            await InferenceClients(Settings(), http).transcribe(wav(), language=language)
    assert error.value.code == "asr_language"


def test_empty_asr_language_normalizes_to_automatic():
    assert validate_asr_language(None) is None
    assert validate_asr_language("") is None


@pytest.mark.parametrize("finish,content,code", [("length", "partial", "cleaner_incomplete"), ("stop", None, "cleaner_empty"), ("tool_calls", "x", "cleaner_incomplete")])
async def test_cleaner_never_executes_partial_or_reasoning_output(finish, content, code):
    payload = {"choices": [{"finish_reason": finish, "message": {"content": content, "reasoning_content": "PRIVATE REASONING"}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as http:
        with pytest.raises(PipelineError) as error:
            await InferenceClients(Settings(), http).clean("raw")
        assert error.value.code == code


def test_truncated_wav_rejected_before_network():
    with pytest.raises(PipelineError, match="incomplet"):
        validate_wav(wav()[:-10], Settings().max_audio_bytes)


def test_wav_accepts_exact_thirty_seconds_but_not_one_extra_frame():
    assert MAX_AUDIO_SECONDS == 30
    assert Settings().max_audio_bytes == 30 * 16_000 * 2 + 4096
    validate_wav(wav(30 * 16_000), Settings().max_audio_bytes)
    # Header allowance is not an allowance for a longer recording.
    too_long = wav(30 * 16_000 + 1)
    assert len(too_long) < Settings().max_audio_bytes
    with pytest.raises(PipelineError) as error:
        validate_wav(too_long, Settings().max_audio_bytes)
    assert error.value.code == "audio_too_long"


@pytest.mark.parametrize("data,code", [
    (wav(30 * 16_000)[:-2], "audio_incomplete"),
    (wav(30 * 16_000 + 1), "audio_too_long"),
    (wav(31 * 16_000), "audio_too_long"),
])
async def test_invalid_wav_never_reaches_network(data, code):
    def forbidden(request):
        pytest.fail("Invalid audio must be rejected before sending it")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(PipelineError) as error:
            await InferenceClients(Settings(), http).transcribe(data)
    assert error.value.code == code


async def test_pipeline_orders_events_and_passes_only_corrected_text():
    events, calls = [], []
    class ExplicitTestClients:
        async def transcribe(self, audio, *, language=None):
            assert language is None
            calls.append("asr")
            return "Paris non Lyon"
        async def clean(self, text):
            calls.append(("clean", text))
            return "Lyon"
    async def reply(turn, text):
        calls.append(("agent", text))
        return "Résultat"
    async def emit(event):
        events.append(event)
    coordinator = TurnCoordinator(ExplicitTestClients(), emit, reply)
    await (await coordinator.start("test-turn", b"explicit-test-audio"))
    assert calls == ["asr", ("clean", "Paris non Lyon"), ("agent", "Lyon")]
    assert [event["type"] for event in events] == ["status", "transcript", "status", "correction", "status", "complete"]
    assert events[-1]["text"] == "Résultat"
    assert events[1]["elapsed_ms"] >= 0
    assert events[3]["elapsed_ms"] >= 0
    assert set(events[-1]["metrics"]) == {"asr_ms", "cleanup_ms", "agent_ms"}


async def test_cleanup_failure_blocks_agent():
    events = []
    class ExplicitTestClients:
        async def transcribe(self, audio, *, language=None): return "raw"
        async def clean(self, text): raise PipelineError("cleaner_incomplete", "incomplete")
    async def forbidden_reply(turn, text):
        pytest.fail("The agent must not execute raw or incomplete transcripts")
    async def emit(event): events.append(event)
    coordinator = TurnCoordinator(ExplicitTestClients(), emit, forbidden_reply)
    await (await coordinator.start("test-turn", b"test"))
    assert events[-1]["error_code"] == "cleaner_incomplete"
    assert not any(event["type"] == "correction" for event in events)


async def test_new_turn_cancels_prior_asr_and_suppresses_late_events():
    first_started = asyncio.Event()
    cancelled = asyncio.Event()
    events, inputs, asr_inputs = [], [], []
    class ExplicitTestClients:
        async def transcribe(self, audio, *, language=None):
            asr_inputs.append((audio, language))
            if audio == b"first":
                first_started.set()
                try: await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return "second raw"
        async def clean(self, text): return "second cleaned"
    async def reply(turn, text):
        inputs.append(text)
        return "ok"
    async def emit(event): events.append(event)
    coordinator = TurnCoordinator(ExplicitTestClients(), emit, reply)
    await coordinator.start("first", b"first", language="fr")
    await first_started.wait()
    old_turn = coordinator.active
    await (await coordinator.start("second", b"second"))
    await coordinator.event(old_turn, "response_delta", text="stale")
    assert cancelled.is_set()
    assert asr_inputs == [(b"first", "fr"), (b"second", None)]
    assert old_turn.language == "fr"
    assert coordinator.active.language is None
    assert inputs == ["second cleaned"]
    assert not any(event.get("text") == "stale" for event in events)
    assert [event["type"] for event in events if event["turn_id"] == "first"] == ["status", "cancelled"]


async def test_stale_cancel_does_not_cancel_current_turn():
    class ExplicitTestClients:
        async def transcribe(self, audio, *, language=None): await asyncio.Event().wait()
    async def unused(*args): pass
    coordinator = TurnCoordinator(ExplicitTestClients(), unused, unused)
    task = await coordinator.start("current", b"test")
    await coordinator.cancel("stale")
    assert coordinator.active.id == "current"
    assert not task.cancelled()
    await coordinator.cancel("current")
    assert task.cancelled()


async def test_invalid_language_does_not_start_a_pipeline_turn():
    async def forbidden(*args):
        pytest.fail("An invalid language must not create a pipeline task")
    coordinator = TurnCoordinator(None, forbidden, forbidden)
    with pytest.raises(PipelineError) as error:
        await coordinator.start("invalid", b"test", language="de")
    assert error.value.code == "asr_language"
    assert coordinator.active is None
