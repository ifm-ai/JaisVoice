#!/usr/bin/env python3
"""Exercise real Playwright MCP against an explicitly fictitious local page."""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def text_of(result):
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def main():
    handler = partial(SimpleHTTPRequestHandler, directory=str(ROOT / "tests"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".runtime/browsers")
    browsers = list((ROOT / ".runtime/browsers").glob("chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"))
    if not browsers:
        raise RuntimeError("Chromium not installed")
    params = StdioServerParameters(command=shutil.which("node"), args=[str(ROOT / "node_modules/@playwright/mcp/cli.js"), "--executable-path", str(browsers[0]), "--isolated", "--image-responses", "omit", "--codegen", "none"], env=env, cwd=str(ROOT))
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                tools = {tool.name: tool for tool in listing.tools}
                print(json.dumps({name: tool.inputSchema for name, tool in tools.items() if name in {"browser_navigate", "browser_snapshot", "browser_type", "browser_click", "browser_find"}}, ensure_ascii=False, indent=2))
                result = await session.call_tool("browser_navigate", {"url": f"http://127.0.0.1:{server.server_port}/browser_fixture.html"})
                if result.isError:
                    raise RuntimeError(text_of(result))
                print(text_of(result))
                # Action arguments are selected after inspecting the actual installed schema.
                async def action(name, target, **extra):
                    properties = tools[name].inputSchema["properties"]
                    if "target" in properties:
                        args = {"target": target, **extra}
                    else:
                        raise RuntimeError("Installed MCP schema changed; inspect browser_smoke output.")
                    value = await session.call_tool(name, args)
                    if value.isError:
                        raise RuntimeError(text_of(value))
                    return text_of(value)
                async def reference(label):
                    snapshot = await session.call_tool("browser_snapshot", {})
                    content = text_of(snapshot)
                    for line in content.splitlines():
                        if label in line and (match := re.search(r"\[ref=([^\]]+)\]", line)):
                            return match.group(1)
                    raise RuntimeError(f"Element {label!r} missing in actual snapshot: {content}")
                print(await action("browser_type", await reference('textbox "Ville"'), text="Paris"))
                print(await action("browser_click", await reference('button "Rechercher"')))
                print(await action("browser_click", await reference('link "Musée de Paris"')))
                snapshot = await session.call_tool("browser_snapshot", {})
                output = text_of(snapshot)
                assert "HORIZON-742" in output, output
                print("PASS: MCP navigation, typing, click and page reading are functional.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
