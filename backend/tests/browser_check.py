"""Opt-in real headed browser check using the production wrappers and resolver."""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

from livekit.agents import mcp
from horizon_voice.browser import BrowserServer
from horizon_voice.config import Settings
from horizon_voice.pipeline import Turn, TurnCoordinator


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / "tests")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    events = []
    async def emit(event): events.append(event)
    async def unused(*args): pass
    coordinator = TurnCoordinator(None, emit, unused)
    turn = Turn("real-browser-validation")
    coordinator.active = turn
    browser = BrowserServer(Settings(), coordinator, lambda _: turn, "real-browser-validation")
    toolset = mcp.MCPToolset(id="browser", mcp_server=browser)
    try:
        await toolset.setup()
        tools = {tool.id: tool for tool in toolset.tools}
        assert "filename" not in tools["browser_snapshot"].info.raw_schema["parameters"]["properties"]
        async def call(name, args):
            ctx = SimpleNamespace(speech_handle=SimpleNamespace(id="validation", interrupted=False), function_call=SimpleNamespace(call_id=f"validation-{turn.actions}"))
            result = await tools[name](ctx, args)
            assert "[Snapshot](" not in result, result
            print(f"{name}: inline text received ({len(result)} chars)")
            return result
        def ref(text, label):
            for line in text.splitlines():
                if label in line and (match := re.search(r"\[ref=([^\]]+)\]", line)):
                    return match.group(1)
            raise AssertionError(f"Missing {label}: {text}")
        text = await call("browser_navigate", {"url": f"http://127.0.0.1:{server.server_port}/browser_fixture.html"})
        text = await call("browser_type", {"target": ref(text, 'textbox "Ville"'), "text": "Paris"})
        text = await call("browser_click", {"target": ref(text, 'button "Rechercher"')})
        text = await call("browser_click", {"target": ref(text, 'link "Musée de Paris"')})
        assert "HORIZON-742" in text, text
        assert turn.actions == 4
        assert [event["type"] for event in events] == ["tool_start", "tool_end"] * 4
        print("PASS: production MCP wrappers provide inline snapshots after navigate/type/click/read, four real actions.")
    finally:
        await toolset.aclose()
        server.shutdown(); server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
