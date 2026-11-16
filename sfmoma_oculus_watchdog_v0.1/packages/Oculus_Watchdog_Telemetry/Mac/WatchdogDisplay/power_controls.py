import json
from ipaddress import IPv4Address
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Lock
import time

from pikvm import PiKVM, PiKVMError, atx_ready
from windows_power import PowerError, WindowsPower

ACTIONS = {
    "power-on": ("PiKVM", "on"),
    "soft-shutdown": ("Windows", "shutdown"),
    "soft-restart": ("Windows", "restart"),
    "hard-shutdown": ("PiKVM", "off_hard"),
    "hard-restart": ("PiKVM", "reset_hard"),
}
RECOVERY_ACTIONS = {"recovery-on", "recovery-off"}
CONFIG = Path("/Library/Application Support/SFMoMAOculus/HostMonitor/monitor.config.json")
MANUAL_INTERVAL = 15


def atomic_maintenance(directory):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as handle:
        pending = Path(handle.name)
        try:
            json.dump({"enabled": True}, handle)
            handle.flush()
            pending.replace(directory / "maintenance.json")
        finally:
            pending.unlink(missing_ok=True)


class PowerControls:
    def __init__(self, directory, app, read_snapshot, config=CONFIG):
        self.directory = directory
        self.app = app
        self.read_snapshot = read_snapshot
        self.config = config
        self.lock = Lock()
        self.next_action = 0

    def targets(self):
        try:
            config = json.loads(self.config.read_text(encoding="utf-8"))
            return tuple(str(IPv4Address(config[key])) for key in ("windows_ip", "pikvm_ip", "mac_bind_ip"))
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise PowerError("The installed watchdog network settings are unavailable.") from exc

    def windows(self):
        host, _, source = self.targets()
        return WindowsPower(self.app, host, source)

    def readiness(self, snapshot):
        try:
            windows = self.windows()
        except PowerError:
            return {"power_on": False, "hard": False, "soft": False, "reason": "Watchdog network settings are unavailable."}
        if self.lock.locked():
            return {"power_on": False, "hard": False, "soft": False, "reason": "A power request is in progress."}
        if time.monotonic() < self.next_action:
            return {"power_on": False, "hard": False, "soft": False, "reason": "Waiting for the server to start the requested action."}
        if snapshot["state"] != "running":
            return {"power_on": False, "hard": False, "soft": False, "reason": "Waiting for fresh watchdog status."}
        if snapshot.get("ups", {}).get("write_in_progress"):
            return {"power_on": False, "hard": False, "soft": False, "reason": "A UPS power request is in progress."}
        atx = snapshot.get("pikvm", {})
        hard = atx_ready(atx)
        power_on = atx_ready(atx, powered=False)
        server_off = atx.get("state") == "reachable" and atx.get("power_led") is False
        windows_configured = windows.configured()
        soft = windows_configured and not server_off
        notes = []
        if not (hard or power_on):
            notes.append("PiKVM controls need a reachable, idle ATX connection and a known power state.")
        if power_on:
            notes.append("The server is powered off. Use Power on via PiKVM to start it.")
        elif not windows_configured and not server_off:
            notes.append("Graceful controls need Windows SSH setup.")
        elif soft:
            notes.append("Soft power checks Windows SSH and shutdown permissions after confirmation.")
        return {"power_on": power_on, "hard": hard, "soft": soft, "reason": " ".join(notes)}

    def recovery_controls(self, snapshot):
        mode = snapshot.get("automatic_restart")
        if mode == "armed":
            enabled = True
        elif mode in {"dry_run", "disabled", "not_implemented"}:
            enabled = False
        else:
            enabled = None
        can_enable = (snapshot["state"] == "running" and mode == "dry_run"
                      and snapshot.get("recovery", {}).get("network_reset_enabled") is not False
                      and time.monotonic() >= self.next_action)
        return {"enabled": enabled, "can_change": not self.lock.locked()
                and (enabled is True or can_enable)}

    def run_monitor_command(self, *options):
        command = [sys.executable, str(self.config.with_name("mac_host_monitor.py")),
                   "--config", str(self.config), "--state-dir", str(self.directory), *options]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=12)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PowerError("The watchdog could not confirm the recovery change. Check its current status.") from exc
        if result.returncode != 0:
            if options == ("--recovery", "arm") and "Restart Guard to be reporting" in result.stderr:
                raise PowerError("Auto-recover needs the Windows Restart Guard service to be reporting.")
            if options == ("--recovery", "arm") and ("Arming requires" in result.stderr
                                                     or "healthy Windows baseline" in result.stderr):
                raise PowerError("Auto-recover needs fresh watchdog status, a progressing Windows heartbeat, "
                                 "and ready PiKVM ATX.")
            raise PowerError("The watchdog could not save the recovery change. Check its current status.")

    def set_recovery(self, enabled):
        if not enabled:
            self.run_monitor_command("--recovery", "dry-run")
            return "Auto-recover is off. The watchdog is in dry-run."
        if time.monotonic() < self.next_action:
            raise PowerError("Wait for the current power action to settle before enabling auto-recover.")
        self.run_monitor_command("--recovery", "arm")
        try:
            self.run_monitor_command("--maintenance", "off")
        except PowerError:
            self.run_monitor_command("--recovery", "dry-run")
            raise PowerError("The watchdog could not end maintenance. Recovery was returned to dry-run.") from None
        return "Auto-recover is on. Maintenance is off."

    def hold_recovery(self):
        current = self.read_snapshot()
        if current["state"] != "running":
            raise PowerError("Wait for fresh watchdog status.")
        try:
            atomic_maintenance(self.directory)
        except OSError as exc:
            raise PowerError("Could not enable maintenance. No power request was sent.") from exc
        requested_at = time.time()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            latest = self.read_snapshot()
            if (latest["state"] == "running" and latest["maintenance"] is True
                    and not latest.get("ups", {}).get("write_in_progress")
                    and latest["updated_unix"] > max(current["updated_unix"], requested_at)):
                return
            time.sleep(0.15)
        raise PowerError("The watchdog has not confirmed maintenance and idle UPS power controls. No manual request was sent.")

    @staticmethod
    def check_pikvm(api, operation):
        atx = api.probe()
        if operation == "on":
            if atx_ready(atx):
                raise PowerError("The show server is already powered on.")
            if not atx_ready(atx, powered=False):
                raise PowerError("To power on, PiKVM must be reachable, idle and show the server powered off.")
        elif not atx_ready(atx):
            raise PowerError("PiKVM must be reachable, idle and show the server powered on.")

    def act(self, action):
        if action not in ACTIONS and action not in RECOVERY_ACTIONS:
            raise PowerError("Unknown power action.")
        if not self.lock.acquire(blocking=False):
            raise PowerError("A power request is in progress.")
        try:
            if action in RECOVERY_ACTIONS:
                return self.set_recovery(action == "recovery-on")
            if time.monotonic() < self.next_action:
                raise PowerError("Check the server before sending another power request.")
            channel, operation = ACTIONS[action]
            host, ip, source = self.targets()
            if channel == "Windows":
                windows = WindowsPower(self.app, host, source)
                windows.send("status")
                perform = lambda: windows.send(operation)
            else:
                api = PiKVM(ip, source, self.directory / "pikvm.credentials.json")
                self.check_pikvm(api, operation)

                def perform():
                    self.check_pikvm(api, operation)
                    try:
                        api.request("POST", f"/api/atx/power?action={operation}&wait=0", api.read_credentials())
                    except PiKVMError as exc:
                        raise PowerError(f"PiKVM did not confirm the request ({exc}). Check the server before retrying.") from exc

            self.hold_recovery()
            self.next_action = time.monotonic() + MANUAL_INTERVAL
            perform()
            if action == "power-on":
                return "PiKVM accepted the power-on request. Watch for the power LED and Windows heartbeat. Maintenance is on."
            return f"{channel} accepted the request. Check the server for completion. Maintenance is on."
        finally:
            self.lock.release()
