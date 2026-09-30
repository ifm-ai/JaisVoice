from types import SimpleNamespace

import pytest
from livekit.agents import llm

from horizon_voice.browser import SnapshotResolver


def context(link):
    return SimpleNamespace(result=SimpleNamespace(content=[SimpleNamespace(text=f"### Page\n- Page URL: http://localhost/test\n### Snapshot\n- [Snapshot]({link})")]))


def test_automatic_snapshot_is_inlined_from_owned_directory(tmp_path):
    output = tmp_path / "browser-output" / "room"
    output.mkdir(parents=True)
    snapshot = output / "page-2026-09-15T15-20-23.yml"
    snapshot.write_text('- textbox "Ville" [ref=e2]\n- button "Rechercher" [ref=e3]', encoding="utf-8")
    result = SnapshotResolver(tmp_path, output)(context(snapshot.relative_to(tmp_path)))
    assert 'textbox "Ville" [ref=e2]' in result
    assert 'button "Rechercher" [ref=e3]' in result
    assert "[Snapshot](" not in result


@pytest.mark.parametrize("kind", ["outside", "symlink", "arbitrary_name"])
def test_snapshot_reader_rejects_unowned_or_arbitrary_files(tmp_path, kind):
    output = tmp_path / "room"
    output.mkdir()
    outside = tmp_path / "page-secret.yml"
    outside.write_text("must never be returned")
    if kind == "symlink":
        target = output / "page-link.yml"
        target.symlink_to(outside)
    elif kind == "arbitrary_name":
        target = output / "secrets.json"
        target.write_text("must never be returned")
    else:
        target = outside
    with pytest.raises(llm.ToolError, match="could not be loaded"):
        SnapshotResolver(tmp_path, output)(context(target))
