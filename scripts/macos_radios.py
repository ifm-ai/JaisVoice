#!/usr/bin/env python3
"""Read radio state or change both radios under the user's authorization.

No shell, elevation, installation, paired-device listing or permission bypass.
`--confirmed` is an accidental-use guard, NOT a replacement for user authorization.
JAIS Voice passes it under the user's explicit global authorization; macOS
privacy permissions remain enforced by the operating system.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time

if __package__:
    from .prepare_macos_tools import TOOLS_ROOT, installed_manifest
else:
    from prepare_macos_tools import TOOLS_ROOT, installed_manifest


NETWORKSETUP = "/usr/sbin/networksetup"
MIN_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}


class RadioError(ValueError):
    pass


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise RadioError(message)


def empty_result(action: str) -> dict:
    return {"ok": False, "action": action, "wifi_interface": None,
            "wifi_enabled": None, "bluetooth_enabled": None, "both_radios_off": None,
            "errors": [], "rollback": {"attempted": False, "verified": None}}


def update_derived(result: dict) -> None:
    wifi, bluetooth = result["wifi_enabled"], result["bluetooth_enabled"]
    result["both_radios_off"] = (not wifi and not bluetooth) if wifi is not None and bluetooth is not None else None


class Radios:
    def __init__(self, blueutil: Path, *, runner=subprocess.run, clock=time.monotonic):
        self.blueutil = str(blueutil)
        self.runner = runner
        self.clock = clock

    def run(self, command: list[str], label: str, deadline: float, timeout: float = 6) -> str:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise RadioError(f"{label}: time budget exhausted")
        try:
            result = self.runner(command, capture_output=True, text=True, check=False,
                                 timeout=min(timeout, remaining), env=MIN_ENV)
        except subprocess.TimeoutExpired as error:
            raise RadioError(f"{label}: timed out") from error
        except OSError as error:
            raise RadioError(f"{label}: command unavailable") from error
        if result.returncode:
            hint = "; check macOS Bluetooth permission" if label.startswith("Bluetooth") and result.returncode in (134, -6) else ""
            # Never include raw outputs: hardware-port listings contain addresses.
            raise RadioError(f"{label}: failed (exit {result.returncode}){hint}")
        if len(result.stdout) > 65536:
            raise RadioError(f"{label}: unexpected oversized output")
        return result.stdout.strip()

    def wifi_interface(self, deadline: float) -> str:
        output = self.run([NETWORKSETUP, "-listallhardwareports"], "Wi-Fi interface", deadline, 3)
        is_wifi = False
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Hardware Port:"):
                is_wifi = line in ("Hardware Port: Wi-Fi", "Hardware Port: AirPort")
            elif is_wifi and line.startswith("Device:"):
                interface = line.partition(":")[2].strip()
                if re.fullmatch(r"en[0-9]+", interface):
                    return interface
                raise RadioError("Wi-Fi interface: unsupported device identifier")
        raise RadioError("Wi-Fi interface: no Wi-Fi hardware found")

    def read_states(self, result: dict, deadline: float, *, timeout: float = 3) -> list[str]:
        errors = []
        interface = result["wifi_interface"]
        result["wifi_enabled"] = None
        result["bluetooth_enabled"] = None
        if interface is not None:
            try:
                output = self.run([NETWORKSETUP, "-getairportpower", interface], "Wi-Fi status", deadline, timeout)
                match = re.fullmatch(rf"Wi-Fi Power \({re.escape(interface)}\): (On|Off)", output)
                if not match:
                    raise RadioError("Wi-Fi status: unrecognized response")
                result["wifi_enabled"] = match.group(1) == "On"
            except RadioError as error:
                errors.append(str(error))
        try:
            output = self.run([self.blueutil, "--power"], "Bluetooth status", deadline, timeout)
            if output not in ("0", "1"):
                raise RadioError("Bluetooth status: unrecognized response")
            result["bluetooth_enabled"] = output == "1"
        except RadioError as error:
            errors.append(str(error))
        update_derived(result)
        return errors

    def set_state(self, radio: str, enabled: bool, interface: str, deadline: float, *, timeout: float = 6) -> None:
        command = ([NETWORKSETUP, "-setairportpower", interface, "on" if enabled else "off"]
                   if radio == "wifi" else [self.blueutil, "--power", "1" if enabled else "0"])
        self.run(command, "Wi-Fi change" if radio == "wifi" else "Bluetooth change", deadline, timeout)

    def execute(self, action: str, *, confirmed: bool = False) -> dict:
        result = empty_result(action)
        if action not in ("status", "airplane_on", "airplane_off"):
            result["errors"].append("Unsupported radio action")
            return result
        if action != "status" and not confirmed:
            result["errors"].append("User authorization is required (--confirmed)")
            return result
        started = self.clock()
        # Reserve nine seconds of the 25-second budget for rollback and rereading.
        normal_deadline = started + (10 if action == "status" else 16)
        try:
            result["wifi_interface"] = self.wifi_interface(normal_deadline)
        except RadioError as error:
            result["errors"].append(str(error))
        result["errors"].extend(self.read_states(result, normal_deadline))
        if result["errors"]:
            return result
        if action == "status":
            result["ok"] = True
            return result
        enabled = action == "airplane_off"
        result["requested_enabled"] = enabled
        original = {radio: result[f"{radio}_enabled"] for radio in ("wifi", "bluetooth")}
        attempted = []
        # Disconnect the network last; restore Wi-Fi first when leaving airplane mode.
        order = ("bluetooth", "wifi") if not enabled else ("wifi", "bluetooth")
        for radio in order:
            if original[radio] == enabled:
                continue
            attempted.append(radio)  # A failed/timed-out setter may still have changed state.
            try:
                self.set_state(radio, enabled, result["wifi_interface"], normal_deadline)
            except RadioError as error:
                result["errors"].append(str(error))
                break
        if not result["errors"]:
            result["errors"].extend(self.read_states(result, normal_deadline, timeout=2))
            if not result["errors"] and all(result[f"{radio}_enabled"] == enabled for radio in original):
                result["ok"] = True
                return result
            if not result["errors"]:
                result["errors"].append("Requested radio states were not verified")
        if attempted:
            result["rollback"]["attempted"] = True
            # Best effort only: retain the original failure even if rollback succeeds.
            for radio in reversed(attempted):
                try:
                    self.set_state(radio, original[radio], result["wifi_interface"], started + 21, timeout=3)
                except RadioError as error:
                    result["errors"].append(f"Rollback: {error}")
            result["errors"].extend(self.read_states(result, started + 25, timeout=2))
            result["rollback"]["verified"] = all(result[f"{radio}_enabled"] == original[radio] for radio in original)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = JSONArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status")
    airplane = subcommands.add_parser("airplane")
    airplane.add_argument("state", choices=("on", "off"))
    airplane.add_argument("--confirmed", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.command == "airplane" and not args.confirmed:
            raise RadioError("User authorization is required (--confirmed)")
    except RadioError as error:
        result = empty_result("invalid")
        result["errors"].append(str(error))
        print(json.dumps(result, separators=(",", ":")))
        return 2
    action = "status" if args.command == "status" else f"airplane_{args.state}"
    try:
        installed_manifest(TOOLS_ROOT)
    except (OSError, ValueError):
        result = empty_result(action)
        result["errors"].append("Pinned project-local blueutil is missing or invalid; prepare macOS tools first")
    else:
        result = Radios(TOOLS_ROOT / "bin/blueutil").execute(action, confirmed=getattr(args, "confirmed", False))
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
