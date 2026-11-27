"""Disk image packaging: a volume that is still busy is detached again instead of failing the build."""
import subprocess

import pytest

from scripts import make_dmg


class FakeHdiutil:
    """Answers "Resource busy" (16) a given number of times, then succeeds."""

    def __init__(self, busy):
        self.busy, self.commands = busy, []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        busy = len(self.commands) <= self.busy
        return subprocess.CompletedProcess(command, 16 if busy else 0)


@pytest.fixture
def hdiutil(monkeypatch):
    def install(busy):
        fake = FakeHdiutil(busy)
        monkeypatch.setattr(make_dmg.subprocess, "run", fake)
        return fake
    return install


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(make_dmg.time, "sleep", waited.append)
    return waited


def test_a_free_volume_is_detached_once_without_force(hdiutil, sleeps):
    fake = hdiutil(busy=0)

    make_dmg.detach("/dev/disk7")

    assert fake.commands == [["/usr/bin/hdiutil", "detach", "/dev/disk7", "-quiet"]]
    assert sleeps == []


def test_a_busy_volume_is_forced_until_it_detaches(hdiutil, sleeps):
    # the case of the failed release build: the plain detach and the first forced one are both refused
    fake = hdiutil(busy=2)

    make_dmg.detach("/dev/disk7", delay=5.0)

    assert [command[-1] for command in fake.commands] == ["-quiet", "-force", "-force"]
    assert all(command[:3] == ["/usr/bin/hdiutil", "detach", "/dev/disk7"] for command in fake.commands)
    assert sleeps == [5.0, 5.0]


def test_a_volume_that_stays_busy_fails_after_the_last_attempt(hdiutil, sleeps):
    fake = hdiutil(busy=100)

    with pytest.raises(RuntimeError, match="could not detach /dev/disk7 after 4 attempts"):
        make_dmg.detach("/dev/disk7", attempts=4, delay=1.0)

    assert len(fake.commands) == 4
    assert sleeps == [1.0, 1.0, 1.0]  # no wait after the last attempt
