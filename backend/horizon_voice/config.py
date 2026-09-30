from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_AUDIO_SECONDS = 30
ASR_LANGUAGES = frozenset({"ar", "msa", "fr", "en", "es", "it"})


def project_root() -> Path:
    return Path(os.environ.get("HORIZON_PROJECT_ROOT", os.environ.get("HORIZON_PROJECT_DIR", Path(__file__).resolve().parents[2])))


@dataclass(frozen=True)
class Settings:
    asr_url: str = field(default_factory=lambda: os.environ.get("HORIZON_ASR_URL", "http://127.0.0.1:8790/transcribe"))
    asr_health_url: str = field(default_factory=lambda: os.environ.get("HORIZON_ASR_HEALTH_URL", "http://127.0.0.1:8790/health"))
    mlx_url: str = field(default_factory=lambda: os.environ.get("HORIZON_MLX_URL", "http://127.0.0.1:8788"))
    livekit_url: str = field(default_factory=lambda: os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"))
    api_key: str = field(default_factory=lambda: os.environ.get("LIVEKIT_API_KEY", "devkey"))
    api_secret: str = field(default_factory=lambda: os.environ.get("LIVEKIT_API_SECRET", "secret"))
    control_url: str = field(default_factory=lambda: os.environ.get("HORIZON_CONTROL_URL", "http://127.0.0.1:8787"))
    cleaner_reasoning_effort: str = field(default_factory=lambda: os.environ.get("HORIZON_CLEANER_REASONING_EFFORT", "low"))
    agent_reasoning_effort: str = field(default_factory=lambda: os.environ.get("HORIZON_AGENT_REASONING_EFFORT", "medium"))
    runtime_dir: Path = field(default_factory=lambda: Path(os.environ.get("HORIZON_RUNTIME_DIR", project_root() / ".runtime")))
    max_audio_bytes: int = 16_000 * 2 * MAX_AUDIO_SECONDS + 4096
    max_actions: int = 50
    agent_name: str = "horizon-voice"

    def __post_init__(self) -> None:
        if self.cleaner_reasoning_effort not in {"low", "medium", "high"} or self.agent_reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("K2 reasoning effort must be low, medium or high")

    @property
    def node(self) -> str:
        return os.environ.get("HORIZON_NODE", "node")

    @property
    def playwright_cli(self) -> Path:
        return Path(os.environ.get("HORIZON_PLAYWRIGHT_CLI", project_root() / "node_modules/@playwright/mcp/cli.js"))
