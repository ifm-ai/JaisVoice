import asyncio
import json

import httpx
import openai
import pytest
from livekit.agents import APIConnectOptions, llm

from horizon_voice.agent import CURRENT_TURN, HorizonLLM
from horizon_voice.clients import InferenceClients, PipelineError
from horizon_voice.config import Settings
from horizon_voice.pipeline import Turn, TurnCoordinator
from k2_runtime.gateway import CompletionRequest, Worker


@pytest.mark.parametrize("role", ["k2-cleaner", "k2-agent"])
@pytest.mark.parametrize("size", ["1B", "4B"])
def test_checkpoint_selection_preserves_role_context_and_checkpoint_framing(role, size):
    worker = Worker()
    try:
        worker.models = {
            "k2-cleaner": (object(), object(), {"max_position_embeddings": 131072},
                           {"display_name": "MBZUAI · 1B", "context_limit": 4096, "tool_call_format": "xml"}),
            "k2-agent": (object(), object(), {"max_position_embeddings": 32768},
                         {"display_name": "MBZUAI · 4B", "context_limit": 16384, "tool_call_format": "xml_typed"}),
        }
        body = CompletionRequest(model=role, model_size=size, messages=[{"role": "user", "content": "test"}])
        chosen = worker.resolve_model(body)
        source = worker.models["k2-cleaner" if size == "1B" else "k2-agent"]
        assert chosen[:3] == source[:3]
        assert chosen[3]["context_limit"] == (4096 if role == "k2-cleaner" else 16384)
        assert chosen[3]["tool_call_format"] == source[3]["tool_call_format"]
        assert worker.models["k2-cleaner"][3]["context_limit"] == 4096
        del worker.models["k2-agent" if size == "1B" else "k2-cleaner"]
        missing = "4B" if size == "1B" else "1B"
        with pytest.raises(ValueError, match="pas disponible"):
            worker.resolve_model(body.model_copy(update={"model": next(iter(worker.models)), "model_size": missing}))
    finally:
        worker.executor.shutdown()


@pytest.mark.parametrize("audio", [False, True])
async def test_all_pairs_are_sent_to_cleaner_and_attached_to_each_turn(audio):
    requests, replies, events = [], [], []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Bonjour"}}]})
    async def emit(event): events.append(event)
    async def reply(turn, text):
        replies.append((turn.cleaner_model, turn.agent_model))
        return "Bonjour"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        clients = InferenceClients(Settings(), http)
        async def transcribe(data, *, language=None): return "Bonjour"
        clients.transcribe = transcribe
        coordinator = TurnCoordinator(clients, emit, reply)
        pairs = [(a, b) for a in ("1B", "4B") for b in ("1B", "4B")]
        for index, (a, b) in enumerate(pairs):
            method = coordinator.start if audio else coordinator.start_text
            await (await method(str(index), b"audio" if audio else "Bonjour", cleaner_model=a, agent_model=b))
            assert events[-1]["type"] == "complete"
        await (await coordinator.start_text("legacy", "Bonjour"))
        assert replies == pairs + [(None, None)]
        assert [r.get("model_size") for r in requests] == [a for a, _ in pairs] + [None]
        assert all(r["model"] == "k2-cleaner" and "tools" not in r for r in requests)
        before = len(requests)
        with pytest.raises(PipelineError):
            await coordinator.start_text("invalid", "Bonjour", agent_model="7B")
        assert len(requests) == before


async def test_agent_model_choice_is_request_scoped_and_kept_across_tool_rounds():
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        payload = {"id": "test", "object": "chat.completion.chunk", "created": 0, "model": "k2-agent",
                   "choices": [{"index": 0, "delta": {"content": "Bonjour"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = openai.AsyncOpenAI(api_key="test", base_url="http://fixture/v1", http_client=http)
        model = HorizonLLM(model="k2-agent", client=client)
        async def call(size):
            token = CURRENT_TURN.set(Turn("test", agent_model=size))
            try:
                for _ in range(2):
                    async with model.chat(chat_ctx=llm.ChatContext(), conn_options=APIConnectOptions(max_retry=0)) as stream:
                        async for _ in stream: pass
            finally:
                CURRENT_TURN.reset(token)
        await asyncio.gather(call("1B"), call("4B"))
        await call(None)
    assert [r.get("model_size") for r in requests].count("1B") == 2
    assert [r.get("model_size") for r in requests].count("4B") == 2
    assert [r.get("model_size") for r in requests].count(None) == 2


@pytest.mark.parametrize("size", ["1B", "4B"])
def test_cleanup_and_agent_share_identical_weight_and_tokenizer_instances(size):
    worker = Worker()
    try:
        worker.models = {
            "k2-cleaner": (object(), object(), {"max_position_embeddings":131072}, {"display_name":"MBZUAI · 1B", "context_limit":4096}),
            "k2-agent": (object(), object(), {"max_position_embeddings":32768}, {"display_name":"MBZUAI · 4B", "context_limit":16384}),
        }
        requests = [CompletionRequest(model=role, model_size=size, messages=[{"role":"user","content":"test"}]) for role in ("k2-cleaner","k2-agent")]
        cleaner, agent = [worker.resolve_model(request) for request in requests]
        assert cleaner[0] is agent[0]
        assert cleaner[1] is agent[1]
        assert cleaner[2] is agent[2]
        assert cleaner[3]["context_limit"] == 4096
        assert agent[3]["context_limit"] == 16384
    finally:
        worker.executor.shutdown()
