"""Jais 3 native message framing translated into OpenAI-compatible deltas."""
from __future__ import annotations
import copy
import json
import re
import uuid
from jsonschema import Draft202012Validator

THINK_END_BY_EFFORT = {"low": "</ifm|think_faster>", "medium": "</ifm|think_fast>", "high": "</ifm|think>"}
TOOLS_START = "<ifm|tool_calls>"
TOOLS_END = "</ifm|tool_calls>"


def normalize_messages(messages):
    messages = copy.deepcopy(messages)
    for message in messages:
        if message.get("role") == "developer":
            message["role"] = "system"
        if message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Unsupported message role")
        content = message.get("content")
        if isinstance(content, list):
            if any(part.get("type") != "text" for part in content):
                raise ValueError("This endpoint accepts text-only messages")
            message["content"] = "\n".join(part.get("text", "") for part in content)
        elif content is None:
            message["content"] = ""
        elif not isinstance(content, str):
            raise ValueError("Message content must be text")
        if message.get("role") == "assistant":
            # The chat template requires a thinking field, even if empty.
            message["think_faster"] = message.get("think_faster", message.get("reasoning_content") or "")
            for call in message.get("tool_calls", []):
                args = call["function"].get("arguments", {})
                if isinstance(args, str):
                    args = json.loads(args, strict=False)
                if not isinstance(args, dict):
                    raise ValueError("Tool arguments must be a JSON object")
                call["function"]["arguments"] = args
    return messages


def _safe_prefix(buffer, marker):
    """Release everything except a possible split control-marker prefix."""
    for count in range(min(len(buffer), len(marker) - 1), 0, -1):
        if buffer.endswith(marker[:count]):
            return buffer[:-count], buffer[-count:]
    return buffer, ""


def _safe_prefix_any(buffer, markers):
    held = max((len(_safe_prefix(buffer, marker)[1]) for marker in markers), default=0)
    return (buffer[:-held], buffer[-held:]) if held else (buffer, '')


class JaisParser:
    def __init__(self, tools=None, reasoning_effort="low"):
        self.mode = "reasoning"
        self.buffer = ""
        self.content = ""
        self.reasoning = ""
        self.calls = []
        self.allowed_tools = {tool["function"]["name"] for tool in tools or []}
        self.tool_schemas = {tool["function"]["name"]: tool["function"].get("parameters", {"type": "object"}) for tool in tools or []}
        self.think_end = THINK_END_BY_EFFORT[reasoning_effort]

    def parse_call(self, text):
        text = text.strip()
        if text.startswith('{'):
            call = json.loads(text, strict=False)
        else:
            prefix, separator, rest = text.partition('<ifm|arg_key>')
            name = prefix.strip()
            if name not in self.allowed_tools:
                raise ValueError('Model emitted an unknown tool')
            args = {}
            if separator:
                rest = separator + rest
                pattern = r'<ifm\|arg_key>(.*?)</ifm\|arg_key>\s*(?:<ifm\|arg_type>.*?</ifm\|arg_type>\s*)?<ifm\|arg_value>(.*?)</ifm\|arg_value>'
                matches = list(re.finditer(pattern, rest, re.DOTALL))
                if not matches or re.sub(pattern, '', rest, flags=re.DOTALL).strip():
                    raise ValueError('Malformed native XML arguments')
                properties = self.tool_schemas[name].get('properties', {})
                for match in matches:
                    key, value = match.group(1).strip(), match.group(2)
                    if key in args:
                        raise ValueError('Duplicate tool argument')
                    spec = properties.get(key, {})
                    kind = spec.get('type')
                    branches = spec.get('anyOf', spec.get('oneOf', []))
                    string_only = kind == 'string' or (branches and all(branch.get('type') in {'string', 'null'} for branch in branches))
                    if string_only:
                        args[key] = value
                    else:
                        args[key] = json.loads(value, strict=False)
            call = {'name': name, 'arguments': args}
        if not isinstance(call, dict) or call.get('name') not in self.allowed_tools or not isinstance(call.get('arguments'), dict):
            raise ValueError('Model emitted an unknown tool or invalid arguments')
        Draft202012Validator(self.tool_schemas[call['name']]).validate(call['arguments'])
        return call

    def feed(self, text):
        self.buffer += text
        events = []
        while self.buffer:
            if self.mode == "reasoning":
                # Real generations sometimes close a low/medium opening with
                # the generic </ifm|think> alias. Recognize the documented family
                # while still withholding all text until a complete close tag.
                endings = [(self.buffer.find(marker), marker) for marker in THINK_END_BY_EFFORT.values() if marker in self.buffer]
                if endings:
                    _, marker = min(endings)
                    thought, self.buffer = self.buffer.split(marker, 1)
                    self.reasoning += thought
                    if thought:
                        events.append({"reasoning_content": thought})
                    self.mode = "content"
                    continue
                visible, self.buffer = _safe_prefix_any(self.buffer, THINK_END_BY_EFFORT.values())
                self.reasoning += visible
                if visible:
                    events.append({"reasoning_content": visible})
                break
            if self.mode == "content":
                if TOOLS_START in self.buffer:
                    visible, self.buffer = self.buffer.split(TOOLS_START, 1)
                    self.content += visible
                    if visible:
                        events.append({"content": visible})
                    self.mode = "tools"
                    continue
                visible, self.buffer = _safe_prefix(self.buffer, TOOLS_START)
                self.content += visible
                if visible:
                    events.append({"content": visible})
                break
            if self.mode == "tools":
                if TOOLS_END not in self.buffer:
                    break
                block, self.buffer = self.buffer.split(TOOLS_END, 1)
                matches = list(re.finditer(r"<ifm\|tool_call>(.*?)</ifm\|tool_call>", block, re.DOTALL))
                residue = re.sub(r"<ifm\|tool_call>.*?</ifm\|tool_call>", "", block, flags=re.DOTALL)
                if not matches or residue.strip():
                    raise ValueError("Malformed tool-call block")
                calls = []
                for match in matches:
                    # The model may put literal line breaks in string-valued arguments.
                    # JSON's non-strict string mode preserves the same structure;
                    # re-encoding below escapes controls for the OpenAI transport.
                    call = self.parse_call(match.group(1))
                    calls.append({"index": len(self.calls) + len(calls), "id": "call_" + uuid.uuid4().hex[:20], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}})
                self.calls.extend(calls)
                events.append({"tool_calls": calls})
                self.mode = "content"
        return events

    def finish(self, finish_reason):
        if finish_reason == "length":
            raise ValueError("The generation reached its token limit before completing")
        if self.mode != "content":
            raise ValueError("The model returned incomplete reasoning or tool-call markup")
        # A partial marker must never leak into a final answer.
        if self.buffer.strip():
            raise ValueError("The model returned an incomplete control marker")
        if not self.content.strip() and not self.calls:
            raise ValueError("The model returned no final answer")
        return "tool_calls" if self.calls else "stop"
