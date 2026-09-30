from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from livekit.agents import RunContext, function_tool, llm, mcp

from .config import Settings, project_root
from .pipeline import Turn, TurnCoordinator


ALLOWED_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_type", "browser_press_key", "browser_select_option", "browser_wait_for",
    "browser_tabs", "browser_fill_form", "browser_find",
}


class SnapshotResolver:
    """Expand only MCP-generated snapshot files owned by this browser session."""

    LINK = re.compile(r"(?m)^### Snapshot\r?\n- \[Snapshot\]\(([^)\r\n]+)\)")
    NAME = re.compile(r"page-[A-Za-z0-9_.-]+\.yml")

    def __init__(self, workspace: Path, output_dir: Path):
        self.workspace = workspace.resolve()
        self.output_dir = output_dir.resolve()

    def __call__(self, context: mcp.MCPToolResultContext) -> str:
        text = "\n".join(part.text for part in context.result.content if hasattr(part, "text"))
        def inline(match):
            candidate = Path(match.group(1))
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(self.output_dir) or not self.NAME.fullmatch(resolved.name):
                    raise ValueError("Snapshot outside the session output directory")
                # Reject a symlink swap at the final component, and bound all I/O.
                descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as source:
                    contents = source.read(60001)
                truncated = len(contents) > 60000
                snapshot = contents[:60000].decode("utf-8", errors="replace")
                return "### Snapshot\n```yaml\n" + snapshot + ("\n[Snapshot abbreviated]" if truncated else "") + "\n```"
            except (OSError, ValueError) as exc:
                raise llm.ToolError("The action completed but its snapshot could not be loaded. Call browser_snapshot without a filename to inspect the current page.") from exc
        text = self.LINK.sub(inline, text)
        # Keep web content bounded for the local model's 16k-token context.
        return text[:18000] + ("\n[Page abrégée ; utilisez browser_find ou browser_snapshot avec une cible précise.]" if len(text) > 18000 else "")


class BrowserServer(mcp.MCPServerStdio):
    """MCP tools with exact per-speech ownership, serial execution and UI events."""

    def __init__(self, settings: Settings, coordinator: TurnCoordinator, speech_turn: Callable[[str], Turn | None], room_id: str):
        if not settings.playwright_cli.is_file():
            raise RuntimeError("Playwright MCP manque. Exécutez la préparation de l’application.")
        environment = dict(os.environ)
        environment.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(settings.runtime_dir / "browsers"))
        output_dir = settings.runtime_dir / "browser-output" / room_id
        super().__init__(
            command=settings.node,
            args=[str(settings.playwright_cli), "--browser", "chromium", "--isolated", "--image-responses", "omit", "--codegen", "none", "--output-dir", str(output_dir)],
            env=environment,
            cwd=project_root(),
            client_session_timeout_seconds=60,
            tool_result_resolver=SnapshotResolver(project_root(), output_dir),
        )
        self.coordinator = coordinator
        self.speech_turn = speech_turn
        self.max_actions = settings.max_actions
        self._action_lock = asyncio.Lock()
        self._calls: dict[str, set[asyncio.Task]] = {}

    async def cancel_turn(self, turn: Turn) -> None:
        calls = tuple(self._calls.get(turn.id, ()))
        for task in calls:
            task.cancel()
        await asyncio.gather(*calls, return_exceptions=True)

    async def list_tools(self, *, tool_options=None):
        original = await super().list_tools()
        return [self._wrap(tool) for tool in original if tool.id in ALLOWED_TOOLS]

    def _wrap(self, original: llm.RawFunctionTool) -> llm.RawFunctionTool:
        async def call(ctx: RunContext, raw_arguments: dict[str, Any]):
            if original.id == "browser_snapshot" and "filename" in raw_arguments:
                raise llm.ToolError("Return the snapshot inline; filename is not supported.")
            turn = self.speech_turn(ctx.speech_handle.id)
            if turn is None or not self.coordinator.is_current(turn):
                raise llm.ToolError("This request has been cancelled; do not continue.")
            async with self._action_lock:
                if turn.clarification_question:
                    raise llm.StopResponse()
                if not self.coordinator.is_current(turn) or ctx.speech_handle.interrupted:
                    raise llm.ToolError("This request has been cancelled; do not continue.")
                if turn.actions >= self.max_actions:
                    raise llm.ToolError(f"The {self.max_actions}-action limit has been reached. Summarize what you found and explain what remains.")
                turn.actions += 1
                tool_id = ctx.function_call.call_id
                began = time.monotonic()
                await self.coordinator.event(turn, "tool_start", tool_id=tool_id, tool_name=original.id, arguments=raw_arguments)
                async def run_with_state():
                    result = await original(raw_arguments)
                    # MCP 0.0.81 omits a snapshot for typing and some other
                    # actions. Capture the resulting state as part of that
                    # action, without asking the model to guess fresh refs.
                    if original.id not in {"browser_snapshot", "browser_find"} and "### Snapshot" not in str(result):
                        if self._client is None:
                            raise llm.ToolError("The browser disconnected before its resulting state could be read.")
                        snapshot = await self._client.call_tool("browser_snapshot", {})
                        if snapshot.isError:
                            raise llm.ToolError("The browser action completed but its resulting page could not be read.")
                        state = self._tool_result_resolver(mcp.MCPToolResultContext(tool_name="browser_snapshot", arguments={}, result=snapshot))
                        if asyncio.iscoroutine(state):
                            state = await state
                        result = str(result) + "\n" + str(state)
                    return result
                request = asyncio.create_task(run_with_state())
                self._calls.setdefault(turn.id, set()).add(request)
                try:
                    result = await request
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self.coordinator.event(turn, "tool_end", tool_id=tool_id, tool_name=original.id, result=str(exc)[:2500], error_code="browser_action_failed", elapsed_ms=round((time.monotonic() - began) * 1000))
                    raise
                finally:
                    self._calls.get(turn.id, set()).discard(request)
                    if not self._calls.get(turn.id):
                        self._calls.pop(turn.id, None)
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                await self.coordinator.event(turn, "tool_end", tool_id=tool_id, tool_name=original.id, result=text[:2500], elapsed_ms=round((time.monotonic() - began) * 1000))
                return result
        schema = copy.deepcopy(original.info.raw_schema)
        if original.id == "browser_snapshot":
            schema["parameters"].get("properties", {}).pop("filename", None)
            if "required" in schema["parameters"]:
                schema["parameters"]["required"] = [name for name in schema["parameters"]["required"] if name != "filename"]
        return function_tool(call, raw_schema=schema)
