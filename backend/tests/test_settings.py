import pytest

from horizon_voice.config import Settings


def test_reasoning_effort_options_are_independent(monkeypatch):
    monkeypatch.setenv("HORIZON_CLEANER_REASONING_EFFORT", "medium")
    monkeypatch.setenv("HORIZON_AGENT_REASONING_EFFORT", "high")
    settings = Settings()
    assert settings.cleaner_reasoning_effort == "medium"
    assert settings.agent_reasoning_effort == "high"


def test_unknown_reasoning_effort_is_rejected(monkeypatch):
    monkeypatch.setenv("HORIZON_AGENT_REASONING_EFFORT", "unsupported")
    with pytest.raises(ValueError, match="reasoning effort"):
        Settings()
