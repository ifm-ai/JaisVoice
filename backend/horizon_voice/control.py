from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from typing import Literal
from datetime import timedelta

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from livekit import api
from pydantic import BaseModel, Field

from .config import Settings


class ModelMemoryRequest(BaseModel):
    action: Literal["load", "unload"]


class SessionRequest(BaseModel):
    client_id: str | None = Field(default=None, max_length=100)


async def upstream_health(settings: Settings) -> dict:
    async def probe(http: httpx.AsyncClient, name: str, url: str):
        try:
            response = await http.get(url, timeout=2)
            response.raise_for_status()
            if name in {"models", "asr"}:
                data = response.json()
                return name, data.get("status") == "ready", data
            return name, True, {}
        except (httpx.HTTPError, ValueError):
            return name, False, {}

    async with httpx.AsyncClient(trust_env=False) as http:
        results = await asyncio.gather(
            probe(http, "asr", settings.asr_health_url),
            probe(http, "models", settings.mlx_url + "/health"),
            probe(http, "livekit", settings.livekit_url.replace("ws://", "http://").replace("wss://", "https://")),
            probe(http, "agent", os.environ.get("HORIZON_AGENT_HEALTH_URL", "http://127.0.0.1:8789/")),
        )
    states = {name: ready for name, ready, _ in results}
    model_result = next(data for name, _, data in results if name == "models")
    asr_result = next(data for name, _, data in results if name == "asr")
    ready = all(states.values())
    status = "ready" if ready else ("error" if any(data.get("status") == "error" for data in (model_result, asr_result)) else "starting")
    message = "Prêt" if ready else model_result.get("message", "Démarrage des services locaux…")
    if not ready and states["models"]:
        message = "Connexion de l’agent local…"
    if not states["asr"]:
        message = asr_result.get("message", "Démarrage de JAIS-ASR F5 local…")
    return {"asr": asr_result, "status": status, "message": message, "models": model_result.get("models", {}), "services": states}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    application = FastAPI(title="JAIS Voice", docs_url=None, redoc_url=None)

    @application.get("/health")
    async def health():
        return await upstream_health(settings)

    @application.post("/models/{size}/memory")
    async def model_memory(size: Literal["1B", "4B", "ASR"], request: ModelMemoryRequest):
        try:
            async with httpx.AsyncClient(trust_env=False) as http:
                url = settings.asr_health_url.rsplit("/", 1)[0] + "/memory" if size == "ASR" else settings.mlx_url + f"/models/{size}/memory"
                response = await http.post(url,
                                           json=request.model_dump(), timeout=180)
                if response.status_code != 200:
                    raise HTTPException(response.status_code, response.json().get("detail", "Model operation failed"))
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Le service des modèles ne répond pas.") from exc
        return await upstream_health(settings)

    @application.post("/session")
    async def session(request: SessionRequest | None = None):
        health = await upstream_health(settings)
        if health["status"] != "ready":
            raise HTTPException(503, detail=health["message"])
        identity = "mac-" + str(uuid.uuid4())
        room = "horizon-" + str(uuid.uuid4())
        async with api.LiveKitAPI(settings.livekit_url, settings.api_key, settings.api_secret) as client:
            try:
                await client.room.create_room(api.CreateRoomRequest(name=room, empty_timeout=60, departure_timeout=20, max_participants=2))
                await client.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
                    agent_name=settings.agent_name,
                    room=room,
                    metadata=json.dumps({"client_identity": identity}),
                ))
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await client.room.delete_room(api.DeleteRoomRequest(room=room))
                raise HTTPException(503, detail="Impossible de créer la session vocale locale.") from exc
        token = (api.AccessToken(settings.api_key, settings.api_secret)
                 .with_identity(identity)
                 .with_name("JAIS Voice macOS")
                 .with_ttl(timedelta(hours=12))
                 .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=False, can_subscribe=True, can_publish_data=True))
                 .to_jwt())
        return {"url": settings.livekit_url, "token": token, "room": room}

    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
