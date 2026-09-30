"""Control health forwards per-model startup states without invented progress."""
import httpx
import pytest

from horizon_voice import control
from horizon_voice.config import Settings


@pytest.mark.parametrize("model_status,model_states", [
    ("starting", {"k2-cleaner": "loading", "k2-agent": "waiting"}),
    ("starting", {"k2-cleaner": "ready", "k2-agent": "loading"}),
    ("ready", {"k2-cleaner": "ready", "k2-agent": "ready"}),
    ("error", {"k2-cleaner": "ready", "k2-agent": "error"}),
])
async def test_control_forwards_real_model_states(monkeypatch, model_status, model_states):
    models = {
        alias: {"display_name": "Test " + alias, "repo": "fixture/" + alias, "status": state,
                **({"error": "Explicit load failure"} if state == "error" else {})}
        for alias, state in model_states.items()
    }
    def respond(request):
        if request.url.port == 8788:
            return httpx.Response(200, json={"status": model_status, "message": "Loading fixture", "models": models})
        if request.url.port == 8790:
            return httpx.Response(200, json={"status": "ready", "profile": "F5"})
        return httpx.Response(200, text="ready")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(control.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    health = await control.upstream_health(Settings())
    assert health["models"] == models
    assert health["status"] == model_status
    assert health["services"] == {"models": model_status == "ready", "livekit": True, "agent": True, "asr": True}
