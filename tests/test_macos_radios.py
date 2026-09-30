"""Fake subprocesses only: these tests never access or change Mac radios."""
import json
from pathlib import Path
import subprocess

import pytest

from scripts import macos_radios as radios


class FakeMachine:
    def __init__(self, wifi=True, bluetooth=True):
        self.wifi = wifi
        self.bluetooth = bluetooth
        self.now = 100.0
        self.calls = []
        self.failures = {}
        self.override_outputs = {}

    def clock(self):
        return self.now

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        key = tuple(argv[1:])
        failure = self.failures.get(key)
        if failure:
            if isinstance(failure, list):
                failure = failure.pop(0)
            if failure == "timeout":
                self.now += kwargs["timeout"]
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if failure:
                return subprocess.CompletedProcess(argv, failure, "personal-name and address", "private")
        if key in self.override_outputs:
            output = self.override_outputs[key]
        elif key == ("-listallhardwareports",):
            output = "Hardware Port: Ethernet\nDevice: en7\nEthernet Address: do-not-expose\n\nHardware Port: Wi-Fi\nDevice: en3\nEthernet Address: private\n"
        elif key == ("-getairportpower", "en3"):
            output = f"Wi-Fi Power (en3): {'On' if self.wifi else 'Off'}\n"
        elif key == ("--power",):
            output = f"{int(self.bluetooth)}\n"
        elif key[:2] == ("-setairportpower", "en3"):
            self.wifi = key[2] == "on"
            output = ""
        elif key[:1] == ("--power",) and len(key) == 2:
            self.bluetooth = key[1] == "1"
            output = ""
        else:
            pytest.fail(f"Unexpected subprocess: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    def controller(self):
        return radios.Radios(Path("/project/.runtime/macos-tools/bin/blueutil"), runner=self.run, clock=self.clock)

    def mutations(self):
        return [call[0] for call in self.calls if "-setairportpower" in call[0] or ("--power" in call[0] and len(call[0]) == 3)]


def test_status_detects_wifi_interface_and_outputs_no_personal_data():
    machine = FakeMachine()
    result = machine.controller().execute("status")
    assert result["ok"] is True
    assert result["wifi_interface"] == "en3"
    assert result["wifi_enabled"] is True and result["bluetooth_enabled"] is True
    assert result["both_radios_off"] is False
    assert machine.mutations() == []
    assert "private" not in json.dumps(result) and "do-not-expose" not in json.dumps(result)


@pytest.mark.parametrize("action", ["airplane_on", "airplane_off"])
def test_no_confirmation_means_no_subprocesses(action):
    machine = FakeMachine()
    result = machine.controller().execute(action)
    assert result["ok"] is False
    assert machine.calls == []


def test_airplane_on_disables_both_after_preflight():
    machine = FakeMachine()
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is True and result["both_radios_off"] is True
    assert [call[0][1:] for call in machine.calls[:3]] == [
        ["-listallhardwareports"], ["-getairportpower", "en3"], ["--power"]]
    assert [call[1:] for call in machine.mutations()] == [["--power", "0"], ["-setairportpower", "en3", "off"]]


def test_airplane_off_reenables_both_and_verifies():
    machine = FakeMachine(False, False)
    result = machine.controller().execute("airplane_off", confirmed=True)
    assert result["ok"] is True
    assert result["wifi_enabled"] is True and result["bluetooth_enabled"] is True
    assert [call[1:] for call in machine.mutations()] == [["-setairportpower", "en3", "on"], ["--power", "1"]]


def test_already_desired_state_has_no_mutations_but_is_verified():
    machine = FakeMachine(False, False)
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is True
    assert machine.mutations() == []


@pytest.mark.parametrize("failure", [("-getairportpower", "en3"), ("--power",), ("-listallhardwareports",)])
def test_preflight_failure_prevents_every_mutation(failure):
    machine = FakeMachine()
    machine.failures[failure] = 134
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False and result["errors"]
    assert machine.mutations() == []
    assert "personal-name" not in json.dumps(result)


def test_second_setter_failure_restores_original_states():
    machine = FakeMachine()
    machine.failures[("-setairportpower", "en3", "off")] = 1
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert result["rollback"] == {"attempted": True, "verified": True}
    assert result["wifi_enabled"] is True and result["bluetooth_enabled"] is True
    assert machine.wifi is True and machine.bluetooth is True


def test_rollback_failure_is_reported_not_hidden():
    machine = FakeMachine()
    machine.failures[("-setairportpower", "en3", "off")] = 1
    machine.failures[("--power", "1")] = 1
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert result["rollback"] == {"attempted": True, "verified": False}
    assert result["wifi_enabled"] is True and result["bluetooth_enabled"] is False
    assert any(error.startswith("Rollback:") for error in result["errors"])


def test_failed_setter_that_changed_state_is_still_rolled_back():
    machine = FakeMachine()
    original_run = machine.run

    def side_effect_then_failure(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if argv[1:] == ["--power", "0"]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return result

    result = radios.Radios(Path("/fake/blueutil"), runner=side_effect_then_failure, clock=machine.clock).execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert machine.bluetooth is True
    assert result["rollback"] == {"attempted": True, "verified": True}


def test_unreadable_final_state_is_unknown_not_success():
    machine = FakeMachine()
    machine.failures[("-setairportpower", "en3", "off")] = 1
    machine.failures[("--power",)] = [0, 134]
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert result["bluetooth_enabled"] is None and result["both_radios_off"] is None
    assert result["rollback"]["verified"] is False


def test_original_mixed_states_preserved_on_failed_change():
    machine = FakeMachine(False, True)
    machine.failures[("--power", "0")] = 1
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert result["wifi_enabled"] is False and result["bluetooth_enabled"] is True
    assert all("-setairportpower" not in call for call in machine.mutations())


def test_successful_setter_without_state_change_is_not_success():
    machine = FakeMachine()
    machine.override_outputs[("--power", "0")] = ""
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert "Requested radio states were not verified" in result["errors"]
    assert result["rollback"]["verified"] is True


@pytest.mark.parametrize("key,output", [(("--power",), "unknown"),
    (("-getairportpower", "en3"), "not a power response"),
    (("-listallhardwareports",), "Hardware Port: Wi-Fi\nDevice: en0; dangerous")])
def test_malformed_command_output_does_not_guess_or_mutate(key, output):
    machine = FakeMachine()
    machine.override_outputs[key] = output
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False and result["errors"]
    assert machine.mutations() == []


def test_timeouts_are_bounded_and_rollback_attempted():
    machine = FakeMachine()
    machine.failures[("--power", "0")] = "timeout"
    machine.failures[("--power", "1")] = "timeout"
    result = machine.controller().execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert result["rollback"]["attempted"] is True
    assert machine.now <= 125
    assert all(call[1]["timeout"] <= 10 for call in machine.calls)


def test_worst_case_deadlines_leave_no_unbounded_work():
    machine = FakeMachine()
    original_run = machine.run

    def slow_run(argv, **kwargs):
        machine.now += kwargs["timeout"]
        return original_run(argv, **kwargs)

    result = radios.Radios(Path("/fake/blueutil"), runner=slow_run, clock=machine.clock).execute("airplane_on", confirmed=True)
    assert result["ok"] is False
    assert machine.now <= 125
    assert result["rollback"]["attempted"] is True


def test_exec_arrays_minimal_environment_and_no_elevation():
    machine = FakeMachine()
    machine.controller().execute("airplane_on", confirmed=True)
    for argv, options in machine.calls:
        assert isinstance(argv, list) and not options.get("shell")
        assert all(word not in argv for word in ("sudo", "sh", "-c", "--paired", "--connected"))
        assert options["env"] == radios.MIN_ENV


def test_cli_requires_confirmation_before_manifest_or_runner(monkeypatch, capsys):
    monkeypatch.setattr(radios, "installed_manifest", lambda *args: pytest.fail("no installation access before confirmation"))
    assert radios.main(["airplane", "on"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and "--confirmed" in result["errors"][0]


def test_cli_reports_invalid_install_without_running_any_command(monkeypatch, capsys):
    def missing(*args):
        raise ValueError("checksum changed")

    monkeypatch.setattr(radios, "installed_manifest", missing)
    assert radios.main(["status"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.parametrize("argv", [[], ["unknown"], ["airplane", "maybe"], ["status", "--confirmed"]])
def test_bad_cli_is_json_with_exit_two(argv, capsys):
    assert radios.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
