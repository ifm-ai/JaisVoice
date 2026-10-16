"""The presentations skill's deck.sh turns the model's text into Keynote's arguments; osascript is faked."""
from pathlib import Path
import subprocess

DECK = Path(__file__).resolve().parents[1] / "agent/skills/presentations/deck.sh"


def run(tmp_path: Path, *args: str, text: str = "") -> subprocess.CompletedProcess:
    fake = tmp_path / "osascript"
    # The fake prints the AppleScript's arguments: action, kind, title, body, notes.
    fake.write_text('#!/bin/sh\nshift\nfor a in "$@"; do printf "[%s]\\n" "$a"; done\n')
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "en_US.UTF-8"}
    return subprocess.run(["/bin/zsh", "-f", str(DECK), *args], input=text, capture_output=True, text=True, env=env)


def test_a_slide_takes_title_bullets_and_notes_as_written(tmp_path):
    result = run(tmp_path, "slide", text='A "quoted" $5 title\n• First\n- Second\n\n  * Third - kept\n---\nNotes\n- with a dash\n')
    assert result.returncode == 0
    assert result.stdout == '[slide]\n[bar]\n[A "quoted" $5 title]\n[First\nSecond\nThird - kept]\n[Notes\n- with a dash]\n'


def test_a_new_deck_takes_a_title_and_subtitle(tmp_path):
    assert run(tmp_path, "new", text="Deck\nWhy it matters\n").stdout == "[new]\n[bar]\n[Deck]\n[Why it matters]\n[]\n"


def test_tables_and_charts_take_markdown_rows(tmp_path):
    table = run(tmp_path, "table", text="Compare\n| Feature | CD | VA |\n|---|---|---|\n| Insured | Yes, $250,000 | No |\n")
    assert table.stdout == "[table]\n[bar]\n[Compare]\n[Feature\tCD\tVA\nInsured\tYes, $250,000\tNo]\n[]\n"
    chart = run(tmp_path, "chart", "line", text="Growth\n| | Y1 | Y5 |\nCD | $104,000 | 121,665\n| VA | 1.5% | -2 |\n---\nSay it\n")
    assert chart.stdout == "[chart]\n[line]\n[Growth]\n[\tY1\tY5\nCD\t$104,000\t121,665\nVA\t1.5%\t-2]\n[Say it]\n"


def test_mistakes_are_explained(tmp_path):
    assert "first line must be the title" in run(tmp_path, "slide", text="\n\n").stderr
    assert "Not a number" in run(tmp_path, "chart", "bar", text="T\n| | A |\n| CD | lots |\n").stderr
    assert "header row" in run(tmp_path, "table", text="T\n| only | header |\n").stderr
    assert "with | between" in run(tmp_path, "table", text="T\nno pipes here\n| a | b |\n").stderr
    assert "bar, line or pie" in run(tmp_path, "chart", "radar", text="T\n").stderr
    assert ".pptx" in run(tmp_path, "export", "deck.key").stderr
    assert "Usage" in run(tmp_path, "draw").stderr
    assert run(tmp_path, "export", "Deck.pptx").stdout == "[Deck.pptx]\n"


def test_a_chart_row_needs_one_number_per_category_and_notes_may_be_decorated(tmp_path):
    assert "Row 2 has 2 numbers but the header row has 3 categories" in run(tmp_path, "chart", "bar", text="T\n| | A | B | C |\n| CD | 1 | 2 |\n").stderr
    assert run(tmp_path, "fact", text="7%\nWhat it means\n#---#\nNotes\n").stdout == "[fact]\n[bar]\n[7%]\n[What it means]\n[Notes]\n"
