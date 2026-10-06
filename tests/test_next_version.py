"""Release versions: each push to main bumps the patch above the latest release."""
import pytest

from scripts.next_version import next_version


def test_first_release_uses_the_floor():
    assert next_version("0.1.0", []) == "0.1.0"
    assert next_version("0.1.0", ["v1.1.0-build.3", "nightly"]) == "0.1.0"


def test_each_release_bumps_the_patch():
    assert next_version("0.1.0", ["v0.1.0"]) == "0.1.1"
    assert next_version("0.1.0", ["v0.1.0", "v0.1.2", "v0.1.1"]) == "0.1.3"
    assert next_version("0.1.0", ["v0.1.9"]) == "0.1.10"


def test_raising_the_floor_starts_a_new_series():
    assert next_version("0.2.0", ["v0.1.7"]) == "0.2.0"
    assert next_version("0.2.0", ["v0.1.7", "v0.2.0"]) == "0.2.1"


def test_the_floor_must_be_complete():
    with pytest.raises(ValueError):
        next_version("1.1", [])
