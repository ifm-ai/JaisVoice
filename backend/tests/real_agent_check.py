"""Opt-in real K2 cleanup + LiveKit AgentSession + headed browser validation.

Text input explicitly bypasses ASR for this check; no inference is substituted.
Requires the existing resident MLX gateway. It never starts another MLX worker.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

import httpx
from livekit.agents import mcp
from horizon_voice.agent import HorizonAgent, RoomPipeline, build_agent_session
from horizon_voice.browser import BrowserServer
from horizon_voice.clients import InferenceClients
from horizon_voice.config import Settings
from horizon_voice.pipeline import Turn, TurnCoordinator

DEFAULT_TEXT = "ابحث عن متحف اللوفر في أبو ظبي لا أقصد متحف اللوفر في باريس وافتح الموقع الرسمي"


async def main(args):
    settings = Settings()
    events = []
    began = time.monotonic()
    async def emit(event):
        events.append(event)
        if event["type"] != "response_delta":
            print(json.dumps(event, ensure_ascii=False), flush=True)
    harness = RoomPipeline.__new__(RoomPipeline)
    harness.session = build_agent_session(settings)
    captured = 0
    async def capture_payload(request):
        nonlocal captured
        if request.url.path.endswith("/chat/completions"):
            captured += 1
            payload = json.loads(await request.aread())
            destination = ROOT / f".runtime/validation/real-agent-request-{args.expect}-{captured:02}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Captured actual SDK payload: {destination}", flush=True)
    # Diagnostic hook on the pinned SDK's actual HTTP client; no payload change.
    harness.session.llm._client._client.event_hooks["request"].append(capture_payload)
    harness.session.output.set_audio_enabled(False)
    harness.speech_turns = {}
    harness.coordinator = TurnCoordinator(None, emit, harness.reply)
    turn = Turn("real-k2-browser-validation")
    harness.coordinator.active = turn
    harness.browser = BrowserServer(settings, harness.coordinator, harness.speech_turns.get, turn.id)
    toolset = mcp.MCPToolset(id="browser", mcp_server=harness.browser)
    report = {"real_inference": True, "real_browser": True, "asr_used": False, "cleaner_used": args.clean_text is None, "validation_input": "explicit cleaned text" if args.clean_text is not None else "explicit raw text", "raw": args.text if args.clean_text is None else None, "criterion": args.expect, "passed": False}
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            health = await http.get(settings.mlx_url + "/health", timeout=5)
            health.raise_for_status()
            if health.json().get("status") != "ready":
                raise RuntimeError("The resident MLX gateway is not ready")
            if args.clean_text is None:
                cleanup_started = time.monotonic()
                clean = await InferenceClients(settings, http).clean(args.text)
                report["cleanup_ms"] = round((time.monotonic() - cleanup_started) * 1000)
            else:
                clean = args.clean_text
            report["cleaned"] = clean
            print(json.dumps({"raw": report["raw"], "cleaned": clean, "asr_used": False, "cleaner_used": report["cleaner_used"]}, ensure_ascii=False), flush=True)
        if args.expect == "web" and ("باريس" not in clean or "أبو ظبي" in clean):
            raise RuntimeError("The actual cleaner did not retain the corrected Paris destination")
        await toolset.setup()
        await harness.session.start(agent=HorizonAgent(harness.coordinator, toolset))
        agent_began = time.monotonic()
        answer = await asyncio.wait_for(harness.reply(turn, clean), timeout=args.timeout)
        turn.timings["agent_ms"] = round((time.monotonic() - agent_began) * 1000)
        if turn.failure:
            raise turn.failure
        pages = []
        for event in events:
            if event["type"] == "tool_end" and not event.get("error_code"):
                pages.extend(re.findall(r"Page URL: (https?://\S+)", event.get("result", "")))
        official = [url for url in pages if urlparse(url).hostname in {"louvre.fr", "www.louvre.fr"}]
        report.update(answer=answer, visited_pages=list(dict.fromkeys(pages)), official_pages=list(dict.fromkeys(official)), browser_actions=turn.actions)
        if args.expect == "web":
            if not official:
                raise RuntimeError("No successful browser tool result confirms an official Louvre page was visited")
            if not re.search(r"https?://(?:www\.)?louvre\.fr", answer):
                raise RuntimeError("The final answer did not cite the visited official site")
        elif (
            turn.actions != 0
            or len(answer.strip()) > 200
            or not re.search(r"[\u0600-\u06ff]", answer)
            or re.search(r"[?؟]", answer)
            or set(re.findall(r"\d+", answer)) - set(re.findall(r"\d+", clean))
        ):
            raise RuntimeError("A standalone Arabic statement must get a short Arabic acknowledgement without actions, questions or invented dates; semantic review is still required")
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        await harness.session.aclose()
        await toolset.aclose()
        report["browser_actions"] = turn.actions
        report["browser_executed"] = turn.actions > 0
        report["metrics"] = turn.timings
        report["elapsed_ms"] = round((time.monotonic() - began) * 1000)
        report["events"] = events
        destination = Path(args.report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "events"}, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--clean-text", help="Explicitly bypass the cleaner and ASR to validate only the active local browser agent")
    parser.add_argument("--expect", choices=["web", "acknowledgement"], default="web")
    parser.add_argument("--timeout", type=float, default=420)
    parser.add_argument("--report", default=str(ROOT / ".runtime/validation/real-agent-browser.json"))
    asyncio.run(main(parser.parse_args()))
