"""Submit an actual WAV through the same LiveKit path as the macOS app."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
from livekit import rtc

from .clients import InferenceClients, validate_wav
from .config import Settings


async def run(args: argparse.Namespace) -> None:
    settings = Settings()
    audio = Path(args.file).read_bytes()
    validate_wav(audio, settings.max_audio_bytes)
    async with httpx.AsyncClient(trust_env=False) as http:
        if args.asr_only:
            started = time.monotonic()
            text = await InferenceClients(settings, http).transcribe(audio)
            print(json.dumps({"transcript": text, "elapsed_ms": round((time.monotonic() - started) * 1000)}, ensure_ascii=False))
            return
        response = await http.post(args.control_url + "/session", json={"client_id": "smoke-cli"}, timeout=15)
        response.raise_for_status()
        connection = response.json()
    room = rtc.Room()
    turn_id = str(uuid.uuid4())
    finished: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    agent_joined = asyncio.Event()

    @room.on("participant_connected")
    def connected(participant: rtc.RemoteParticipant):
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_joined.set()

    @room.on("data_received")
    def received(packet: rtc.DataPacket):
        if packet.topic != "horizon.events":
            return
        event = json.loads(packet.data)
        if event.get("turn_id") != turn_id:
            return
        print(json.dumps(event, ensure_ascii=False), flush=True)
        if event["type"] in {"complete", "error", "cancelled"} and not finished.done():
            finished.set_result(event)

    try:
        await room.connect(connection["url"], connection["token"])
        for participant in room.remote_participants.values():
            connected(participant)
        await asyncio.wait_for(agent_joined.wait(), 45)
        writer = await room.local_participant.stream_bytes("recording.wav", total_size=len(audio), mime_type="audio/wav", topic="horizon.audio", attributes={"turn_id": turn_id, "cleaner_model": args.model_size, "agent_model": args.model_size})
        for offset in range(0, len(audio), 16000):
            await writer.write(audio[offset:offset + 16000])
        await writer.aclose()
        result = await asyncio.wait_for(finished, args.timeout)
        if result["type"] != "complete":
            raise RuntimeError(result.get("text", result["type"]))
    finally:
        await room.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--asr-only", action="store_true")
    parser.add_argument("--model-size", choices=["1B", "4B"], default="4B", help="Shared cleanup and agent model")
    parser.add_argument("--control-url", default="http://127.0.0.1:8787")
    parser.add_argument("--timeout", type=float, default=600)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
