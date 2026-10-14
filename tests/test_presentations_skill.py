"""The presentations skill's deck.sh turns the model's text into Keynote's arguments; osascript is faked."""
from pathlib import Path
import subprocess

DECK = Path(__file__).resolve().parents[1] / "agent/skills/presentations/deck.sh"


def run(tmp_path: Path, *args: str, text: str = "") -> subprocess.CompletedProcess:
    fake = tmp_path / "osascript"
    fake.write_text('#!/bin/sh\nshift\nfor a in "$@"; do printf "[%s]\\n" "$a"; done\n')
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "en_US.UTF-8"}
    return subprocess.run(["/bin/zsh", "-f", str(DECK), *args], input=text, capture_output=True, text=True, env=env)


def test_a_slide_takes_title_bullets_and_notes_as_written(tmp_path):
    result = run(tmp_path, "slide", text='A "quoted" $5 title\n• First\n- Second\n\n  * Third - kept\n---\nNotes\n- with a dash\n')
    assert result.returncode == 0
    assert result.stdout == '[A "quoted" $5 title]\n[First\nSecond\nThird - kept]\n[Notes\n- with a dash]\n'


def test_a_new_deck_takes_a_title_and_subtitle(tmp_path):
    assert run(tmp_path, "new", text="Deck\nWhy it matters\n").stdout == "[Deck]\n[Why it matters]\n"


def test_mistakes_are_explained(tmp_path):
    assert "first line must be the title" in run(tmp_path, "slide", text="\n\n").stderr
    assert ".pptx" in run(tmp_path, "export", "deck.key").stderr
    assert "Usage" in run(tmp_path, "draw").stderr
    assert run(tmp_path, "export", "Deck.pptx").stdout == "[Deck.pptx]\n"
