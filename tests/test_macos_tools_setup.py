"""Offline fixtures only: no Bluetooth calls, network, compiler or radio changes."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from scripts import prepare_macos_tools as setup


def archive_bytes(extra=(), include_required=True):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        entries = []
        if include_required:
            entries += [(f"{setup.SOURCE_ROOT}/{name}", b"fixture")
                        for name in ("blueutil.m", "LICENSE.txt", "Makefile")]
        for name, payload in entries + list(extra):
            member = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            if member.isfile():
                member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)
    return result.getvalue()


def test_pinned_checksum():
    assert setup.VERSION == "2.14.0"
    assert setup.SOURCE_URL == "https://github.com/toy/blueutil/archive/refs/tags/v2.14.0.tar.gz"
    assert setup.SOURCE_SHA256 == "8749883d52be4630c0b557656a487432050cc6e5d125f60d5b1efe646797d152"


def test_checksum_rejects_changed_content_and_accepts_expected(monkeypatch):
    fixture = archive_bytes()
    with pytest.raises(ValueError, match="checksum"):
        setup.verified_archive(fixture)
    monkeypatch.setattr(setup, "SOURCE_SHA256", hashlib.sha256(fixture).hexdigest())
    assert setup.verified_archive(fixture) == fixture
    with pytest.raises(ValueError, match="checksum"):
        setup.verified_archive(fixture + b"changed")


def test_compressed_size_limit_precedes_hashing(monkeypatch):
    monkeypatch.setattr(setup, "MAX_ARCHIVE_BYTES", 4)
    with pytest.raises(ValueError, match="size limit"):
        setup.verified_archive(b"12345")


def test_download_is_bounded_and_checksum_verified_without_network(monkeypatch):
    fixture = archive_bytes()
    monkeypatch.setattr(setup, "SOURCE_SHA256", hashlib.sha256(fixture).hexdigest())
    calls = []

    class Response(io.BytesIO):
        headers = {}

        def geturl(self):
            return "https://codeload.github.com/toy/blueutil/tar.gz/refs/tags/v2.14.0"

        def read(self, size=-1):
            calls.append(size)
            return super().read(size)

    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *args, **kwargs: Response(fixture))
    assert setup.download_source() == fixture
    assert calls == [setup.MAX_ARCHIVE_BYTES + 1]


def test_download_rejects_https_downgrade_without_reading(monkeypatch):
    class Response(io.BytesIO):
        headers = {}

        def geturl(self):
            return "http://github.com/source"

        def read(self, size=-1):
            pytest.fail("Insecure source must not be read")

    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="HTTPS"):
        setup.download_source()


def test_regular_source_extraction(tmp_path):
    source = setup.extract_source(archive_bytes(), tmp_path / "source")
    assert source.name == setup.SOURCE_ROOT
    assert (source / "blueutil.m").read_bytes() == b"fixture"
    assert (source / "blueutil.m").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("path", ["/tmp/escaped", "../escaped", "other-root/file",
    f"{setup.SOURCE_ROOT}/../escaped", f"{setup.SOURCE_ROOT}/./file",
    f"{setup.SOURCE_ROOT}//file", f"{setup.SOURCE_ROOT}/back\\slash"])
def test_archive_rejects_unsafe_paths_before_writing(tmp_path, path):
    target = tmp_path / "source"
    with pytest.raises(ValueError, match="Unsafe"):
        setup.extract_source(archive_bytes([(path, b"bad")]), target)
    assert not target.exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                  tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_archive_rejects_links_and_devices(tmp_path, kind):
    member = tarfile.TarInfo(f"{setup.SOURCE_ROOT}/malicious")
    member.type = kind
    member.linkname = "../../escaped"
    with pytest.raises(ValueError, match="Links and special"):
        setup.extract_source(archive_bytes([(member, b"")]), tmp_path / "source")


def test_archive_rejects_duplicate_paths(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        setup.extract_source(archive_bytes([(f"{setup.SOURCE_ROOT}/blueutil.m", b"changed")]), tmp_path)


def test_archive_requires_source_and_license(tmp_path):
    with pytest.raises(ValueError, match="Required source"):
        setup.extract_source(archive_bytes(include_required=False), tmp_path)


def test_archive_rejects_file_directory_collisions(tmp_path):
    with pytest.raises(ValueError, match="collides"):
        setup.extract_source(archive_bytes([(f"{setup.SOURCE_ROOT}/blueutil.m/child", b"bad")]), tmp_path)


@pytest.mark.parametrize("limit,value", [("MAX_MEMBERS", 2), ("MAX_MEMBER_BYTES", 2),
                                       ("MAX_EXPANDED_BYTES", 10)])
def test_archive_resource_caps(tmp_path, monkeypatch, limit, value):
    monkeypatch.setattr(setup, limit, value)
    with pytest.raises(ValueError):
        setup.extract_source(archive_bytes(), tmp_path / "source")
    assert not (tmp_path / "source").exists()


def test_existing_files_and_symlink_destination_preserved(tmp_path):
    target = tmp_path / "source"
    target.mkdir()
    (target / "keep").write_text("mine")
    with pytest.raises(ValueError, match="empty real directory"):
        setup.extract_source(archive_bytes(), target)
    assert (target / "keep").read_text() == "mine"
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="empty real directory"):
        setup.extract_source(archive_bytes(), link)


def fake_install(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup, "download_source", archive_bytes)
    monkeypatch.setattr(setup, "resolve_sdk", lambda override=None: tmp_path / "MacOSX26.5.sdk")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = Path(argv[argv.index("-o") + 1])
        output.write_bytes(b"fake-compiled-binary")
        output.chmod(0o755)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    binary = setup.prepare(tmp_path / "tools")
    return binary, calls


def test_build_uses_only_fixed_compiler_never_executes_tool(monkeypatch, tmp_path):
    binary, calls = fake_install(monkeypatch, tmp_path)
    assert binary == tmp_path / "tools/bin/blueutil"
    assert len(calls) == 1
    command, options = calls[0]
    assert command[0] == "/usr/bin/clang"
    assert command[command.index("-isysroot") + 1] == str(tmp_path / "MacOSX26.5.sdk")
    assert "-arch" in command and "arm64" in command
    assert not options.get("shell", False)
    assert all(word not in command for word in ("sudo", "make", "--power", "--version"))
    assert setup.installed_manifest(tmp_path / "tools")["version"] == setup.VERSION
    assert not list(tmp_path.glob(".prepare-macos-tools-*"))


def test_repeat_build_is_offline_and_does_not_execute(monkeypatch, tmp_path):
    binary, calls = fake_install(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "download_source", lambda: pytest.fail("unexpected network"))
    assert setup.prepare(tmp_path / "tools") == binary
    assert len(calls) == 1


def test_changed_binary_is_never_overwritten(monkeypatch, tmp_path):
    binary, _ = fake_install(monkeypatch, tmp_path)
    binary.write_bytes(b"user change")
    with pytest.raises(ValueError, match="checksum changed"):
        setup.prepare(tmp_path / "tools")
    assert binary.read_bytes() == b"user change"


def test_failed_build_leaves_no_install(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(setup, "download_source", archive_bytes)
    monkeypatch.setattr(setup, "resolve_sdk", lambda override=None: tmp_path / "MacOSX26.5.sdk")

    def failed_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(setup.subprocess, "run", failed_run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.prepare(tmp_path / "tools")
    assert not (tmp_path / "tools").exists()
    assert not list(tmp_path.glob(".prepare-macos-tools-*"))


def test_check_executes_version_only_and_never_downloads(monkeypatch, tmp_path):
    binary, _ = fake_install(monkeypatch, tmp_path)
    monkeypatch.setattr(setup, "download_source", lambda: pytest.fail("unexpected network"))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "2.14.0\n", "")

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    assert setup.check(tmp_path / "tools") == "2.14.0"
    assert calls == [[str(binary), "--version"]]


def test_manifest_pinned_metadata_cannot_be_substituted(monkeypatch, tmp_path):
    fake_install(monkeypatch, tmp_path)
    manifest_file = tmp_path / "tools/manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["source_url"] = "https://example.com/other-source"
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="pinned release"):
        setup.installed_manifest(tmp_path / "tools")


def test_malformed_manifest_preserves_existing_files(monkeypatch, tmp_path):
    binary, _ = fake_install(monkeypatch, tmp_path)
    (tmp_path / "tools/manifest.json").write_text("[]")
    with pytest.raises(ValueError, match="pinned release"):
        setup.prepare(tmp_path / "tools")
    assert binary.read_bytes() == b"fake-compiled-binary"


def fake_sdk(path):
    path.mkdir(parents=True)
    (path / "SDKSettings.plist").write_text("fixture")
    for framework in ("Foundation", "IOBluetooth"):
        (path / f"System/Library/Frameworks/{framework}.framework").mkdir(parents=True)
    return path


def test_compatible_sdk_preferred_without_xcrun(monkeypatch, tmp_path):
    sdk = fake_sdk(tmp_path / "MacOSX26.5.sdk")
    monkeypatch.setattr(setup, "COMPATIBLE_SDK", sdk)
    monkeypatch.setattr(setup.subprocess, "check_output", lambda *args, **kwargs: pytest.fail("unexpected xcrun"))
    assert setup.resolve_sdk() == sdk


def test_sdk_override_takes_precedence(monkeypatch, tmp_path):
    compatible = fake_sdk(tmp_path / "MacOSX26.5.sdk")
    override = fake_sdk(tmp_path / "Chosen SDK.sdk")
    monkeypatch.setattr(setup, "COMPATIBLE_SDK", compatible)
    assert setup.resolve_sdk(override) == override
    command = setup.build_command(tmp_path / "source", tmp_path / "binary", override)
    assert command[command.index("-isysroot") + 1] == str(override)


def test_xcrun_fallback_is_explicit_and_verified(monkeypatch, tmp_path):
    sdk = fake_sdk(tmp_path / "Xcode.sdk")
    monkeypatch.setattr(setup, "COMPATIBLE_SDK", tmp_path / "missing.sdk")
    calls = []

    def fake_output(argv, **kwargs):
        calls.append(argv)
        return str(sdk) + "\n"

    monkeypatch.setattr(setup.subprocess, "check_output", fake_output)
    assert setup.resolve_sdk() == sdk
    assert calls == [["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"]]


@pytest.mark.parametrize("value", ["", "relative/path", "/absolute\n/other"])
def test_invalid_xcrun_result_rejected(monkeypatch, tmp_path, value):
    monkeypatch.setattr(setup, "COMPATIBLE_SDK", tmp_path / "missing.sdk")
    monkeypatch.setattr(setup.subprocess, "check_output", lambda *args, **kwargs: value)
    with pytest.raises(ValueError, match="invalid macOS SDK path"):
        setup.resolve_sdk()


def test_incomplete_override_not_silently_replaced(tmp_path):
    sdk = tmp_path / "incomplete.sdk"
    sdk.mkdir()
    with pytest.raises(ValueError, match="complete macOS SDK"):
        setup.resolve_sdk(sdk)
