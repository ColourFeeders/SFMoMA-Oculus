#!/usr/bin/env python3
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from power_controls import PowerControls
from windows_power import PowerError, private_file

LABEL = "org.sfmoma.oculus.watchdogdisplay"
HOST = "127.0.0.1"
PORT = 19742
ROOT = Path(__file__).resolve().parent
OCULUS = Path.home() / "Library/Application Support/SFMoMAOculus"
WATCHDOG_STATUS = OCULUS / "HostMonitor/status.json"
APP = OCULUS / "WatchdogDisplay"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/display.js": ("display.js", "text/javascript; charset=utf-8"),
    "/display.css": ("display.css", "text/css; charset=utf-8"),
}
MAX_STATUS_BYTES = 128 * 1024
STALE_SECONDS = 10


def selected_fields(obj, names):
    if not isinstance(obj, dict):
        return {}
    return {name: obj.get(name) for name in names
            if isinstance(obj.get(name), (str, bool, int)) or obj.get(name) is None}


def recent_show_events(directory):
    events = []
    for suffix in ("", ".1"):
        try:
            with (directory / ("show-events.jsonl" + suffix)).open("rb") as file:
                size = file.seek(0, os.SEEK_END)
                file.seek(max(0, size - 32768))
                if size > 32768:
                    file.readline(32768)
                lines = file.read(32768).splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                timestamp = entry["at_unix"]
                fields = selected_fields(entry["data"], ("name", "state", "fault", "process_match", "detail"))
                if (entry["event"] == "show_status_changed" and fields.get("name") in ("Observer", "TD_A", "TD_B", "MadMapper", "UPS")
                        and type(timestamp) in (float, int) and 0 < timestamp < 1e11):
                    events.append(dict(fields, at_unix=timestamp))
            except (ValueError, KeyError, TypeError, RecursionError):
                continue
            if len(events) >= 12:
                return events
    return events


def snapshot(path):
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_STATUS_BYTES + 1)
        if len(raw) > MAX_STATUS_BYTES:
            raise ValueError("status too large")
        status = json.loads(raw)
        updated = status["updated_unix"]
        process = status["process_state"]
        if type(updated) not in (float, int) or not math.isfinite(updated) or process not in ("running", "stopped"):
            raise ValueError("invalid watchdog status")
    except FileNotFoundError:
        return {"state": "waiting"}
    except (KeyError, OSError, ValueError, TypeError, RecursionError, OverflowError):
        return {"state": "unreadable"}
    age = time.time() - updated
    if age < 0:
        state = "clock_mismatch"
    elif age > STALE_SECONDS:
        state = "stale"
    else:
        state = process
    return {
        "state": state, "updated_unix": updated, "age_seconds": round(age, 1),
        "version": status.get("version") if type(status.get("version")) is str else None,
        "maintenance": status.get("maintenance") if type(status.get("maintenance")) is bool else None,
        "automatic_restart": status.get("automatic_restart") if type(status.get("automatic_restart")) is str else None,
        "network": selected_fields(status.get("network"), ("state",)),
        "windows": selected_fields(status.get("host_heartbeat"), ("state", "sequence")),
        "pikvm": selected_fields(status.get("pikvm"), ("state", "reason", "enabled", "busy", "power_led", "ip")),
        "ups": selected_fields(status.get("ups"), ("state", "reason", "action", "action_reason",
                                                     "seconds_remaining", "write_in_progress")),
        "recovery": selected_fields(status.get("recovery"), ("state", "restart_guard_ready", "boot_grace_seconds",
                                                              "network_reset_enabled", "retry_reason",
                                                              "outage_seconds_remaining", "reset_in_seconds",
                                                              "last_reset_result", "ups_hold")),
        "show": status.get("show") if type(status.get("show")) is dict else {},
    }


class DisplayHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(4)
        super().setup()

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            self.send_error(400)
            return
        if self.path == "/api/status":
            current = snapshot(self.server.status_path)
            current["power_controls"] = self.server.power.readiness(current)
            current["recovery_controls"] = self.server.power.recovery_controls(current)
            current["show_events"] = recent_show_events(self.server.status_path.parent)
            self.send_json(current)
        elif self.path in ASSETS:
            body, mime = self.server.assets[self.path]
            self.send_payload(body, mime)
        else:
            self.send_error(404)

    def do_POST(self):
        host = f"{HOST}:{self.server.server_port}"
        token = self.headers.get("X-Oculus-Power-Token", "")
        if (self.headers.get("Host") != host or self.headers.get("Origin") != f"http://{host}"
                or not token.isascii()
                or not hmac.compare_digest(token, self.server.power_token)):
            self.send_json({"message": "Open the local watchdog dashboard to send power requests."}, 403)
            return
        if self.path != "/api/power":
            self.send_error(404)
            return
        length = self.headers.get("Content-Length", "0")
        if (self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json"
                or not length.isascii() or not length.isdecimal() or len(length) > 3
                or not 0 < int(length) <= 128):
            self.send_json({"message": "Invalid power request."}, 400)
            return
        try:
            body = json.loads(self.rfile.read(int(length)))
            action = body["action"]
            if type(body) is not dict or set(body) != {"action"} or type(action) is not str:
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError, RecursionError):
            self.send_json({"message": "Invalid power request."}, 400)
            return
        try:
            message = self.server.power.act(action)
        except PowerError as exc:
            self.send_json({"message": str(exc)}, 409)
            return
        self.send_json({"message": message})

    def send_json(self, body, status=200):
        self.send_payload(json.dumps(body, allow_nan=False).encode(), "application/json", status)

    def send_payload(self, body, mime, status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def open_dashboard(port, token):
    try:
        subprocess.run(["/usr/bin/open", f"http://{HOST}:{port}/#power={token}"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PowerError("Could not open the dashboard in the default browser.") from exc


def serve(status_path, port, open_browser):
    assets = {route: ((ROOT / filename).read_bytes(), mime) for route, (filename, mime) in ASSETS.items()}
    with ThreadingHTTPServer((HOST, port), DisplayHandler) as server:
        server.status_path = status_path
        server.power = PowerControls(status_path.parent, APP, lambda: snapshot(status_path))
        server.power_token = secrets.token_urlsafe(32)
        server.assets = assets
        if open_browser:
            with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=APP, delete=False) as handle:
                pending = Path(handle.name)
                try:
                    handle.write(server.power_token)
                    handle.flush()
                    pending.replace(APP / "browser-session")
                finally:
                    pending.unlink(missing_ok=True)
            try:
                open_dashboard(server.server_port, server.power_token)
            except PowerError:
                pass
        server.serve_forever()


def launchctl(*arguments, check=True, quiet=False):
    return subprocess.run(["/bin/launchctl", *arguments], check=check,
                          stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None)


def agent_definition(python, app):
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(app / "display.py"), "--open-browser"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 15,
    }


def bootout(domain):
    service = domain + "/" + LABEL
    if launchctl("print", service, check=False, quiet=True).returncode == 0:
        launchctl("bootout", service)
        deadline = time.monotonic() + 15
        while launchctl("print", service, check=False, quiet=True).returncode == 0:
            if time.monotonic() >= deadline:
                raise RuntimeError("launchd is still unloading the watchdog display. Rerun the installer once it clears.")
            time.sleep(0.2)


def install():
    domain = f"gui/{os.getuid()}"
    if launchctl("print", domain, check=False, quiet=True).returncode != 0:
        raise RuntimeError("Run this in Terminal in your Mac desktop session")
    APP.mkdir(mode=0o700, parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    bootout(domain)
    for filename in ["display.py", "power_controls.py", "windows_power.py", "pikvm.py",
                     *(name for name, _ in ASSETS.values())]:
        with tempfile.NamedTemporaryFile(dir=APP, delete=False) as temporary:
            pending = Path(temporary.name)
        try:
            shutil.copyfile(ROOT / filename, pending)
            pending.replace(APP / filename)
        finally:
            pending.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(dir=PLIST.parent, delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            plistlib.dump(agent_definition(Path(sys.executable).resolve(), APP), temporary)
            temporary.flush()
            pending.replace(PLIST)
        finally:
            pending.unlink(missing_ok=True)
    try:
        launchctl("bootstrap", domain, str(PLIST))
        wait_for_display()
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        bootout(domain)
        PLIST.unlink(missing_ok=True)
        raise
    print(f"Watchdog display installed and set to open at login: http://{HOST}:{PORT}/")


def wait_for_display():
    opener = build_opener(ProxyHandler({}))
    url = f"http://{HOST}:{PORT}/"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=0.5) as response:
                if b"<title>Oculus Watchdog</title>" in response.read(16 * 1024):
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"The display could not start on {HOST}:{PORT}. Check whether that port is already in use.")


def main():
    parser = argparse.ArgumentParser(description="Local status and manual power controls for the Oculus watchdog.")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--install", action="store_true")
    commands.add_argument("--remove", action="store_true")
    commands.add_argument("--check-windows-power", action="store_true")
    commands.add_argument("--open-power-display", action="store_true")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--status-file", type=Path, default=WATCHDOG_STATUS)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if args.install or args.remove:
        if sys.platform != "darwin" or os.geteuid() == 0:
            parser.error("Run the installer in Terminal as the logged-in Mac user, without sudo")
        if args.install:
            install()
        else:
            bootout(f"gui/{os.getuid()}")
            PLIST.unlink(missing_ok=True)
            (APP / "browser-session").unlink(missing_ok=True)
            print("Watchdog display removed from login startup.")
    elif args.open_power_display:
        session = APP / "browser-session"
        if not private_file(session):
            raise PowerError("Install the display or start its login service first.")
        open_dashboard(PORT, session.read_text(encoding="ascii").strip())
    elif args.check_windows_power:
        power = PowerControls(args.status_file.parent, APP, lambda: snapshot(args.status_file))
        power.windows().send("status")
        print("Windows power connection authenticated. Shutdown privilege is available.")
    else:
        serve(args.status_file, args.port, args.open_browser)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, PowerError) as exc:
        sys.exit(str(exc))
