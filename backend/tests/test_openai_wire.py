"""Exercise the installed LiveKit/OpenAI wire parser without loading any model."""
import json

import httpx
import openai
import pytest
from livekit.agents import APIConnectOptions, APIConnectionError, llm
from livekit.plugins import openai as livekit_openai

from k2_runtime.protocol import normalize_messages


def sse(delta, finish=None):
    return "data: " + json.dumps({"id": "wire-test", "object": "chat.completion.chunk", "created": 0, "model": "k2-agent", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


async def collect(payload, *, context=None, tools=None):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=payload.encode(), headers={"content-type": "text/event-stream"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = openai.AsyncOpenAI(api_key="explicit-test", base_url="http://explicit.test/v1", http_client=http, max_retries=0)
    model = livekit_openai.LLM(model="k2-agent", client=client, max_completion_tokens=2048, reasoning_effort="low", parallel_tool_calls=False, _strict_tool_schema=False)
    chunks = []
    try:
        async with model.chat(chat_ctx=context or llm.ChatContext(), tools=tools or [], conn_options=APIConnectOptions(max_retry=0)) as stream:
            async for chunk in stream:
                chunks.append(chunk)
    finally:
        await client.close()
    return requests, chunks


async def test_plugin_parameters_and_reasoning_filter_match_gateway():
    requests, chunks = await collect(sse({"reasoning_content": "private"}) + sse({"content": "answer"}) + sse({}, "stop") + "data: [DONE]\n\n")
    assert requests[0]["max_completion_tokens"] == 2048
    assert requests[0]["reasoning_effort"] == "low"
    assert requests[0]["parallel_tool_calls"] is False
    assert [chunk.delta.content for chunk in chunks if chunk.delta] == ["answer"]


async def test_gateway_sse_error_after_partial_content_is_not_success():
    payload = sse({"content": "partial"}) + 'data: {"error":{"message":"token limit","type":"generation_error","code":"incomplete_generation"}}\n\n'
    with pytest.raises(APIConnectionError):
        await collect(payload)


async def test_three_separate_validated_tool_deltas_and_roundtrip():
    async def implementation(raw_arguments: dict): return "test result"
    tool = llm.function_tool(implementation, raw_schema={"name": "browser_test", "description": "Explicit test", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}})
    context = llm.ChatContext()
    context.add_message(role="user", content="cleaned request")
    context.items.extend([
        llm.FunctionCall(call_id="prior", name="browser_test", arguments='{"query":"old"}'),
        llm.FunctionCallOutput(call_id="prior", name="browser_test", output="source result", is_error=False),
    ])
    payload = ""
    for index in range(3):
        payload += sse({"tool_calls": [{"index": index, "id": f"test-{index}", "type": "function", "function": {"name": "browser_test", "arguments": json.dumps({"query": str(index)})}}]})
    payload += sse({}, "tool_calls") + "data: [DONE]\n\n"
    requests, chunks = await collect(payload, context=context, tools=[tool])
    calls = [call for chunk in chunks if chunk.delta for call in chunk.delta.tool_calls]
    assert [call.call_id for call in calls] == ["test-0", "test-1", "test-2"]
    assert requests[0]["tools"][0]["function"]["name"] == "browser_test"
    history = normalize_messages(requests[0]["messages"])
    assistant = next(message for message in history if message["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["arguments"] == {"query": "old"}
    assert assistant["think_faster"] == ""
    result = next(message for message in history if message["role"] == "tool")
    assert result["tool_call_id"] == "prior"
    assert result["content"] == "source result"
