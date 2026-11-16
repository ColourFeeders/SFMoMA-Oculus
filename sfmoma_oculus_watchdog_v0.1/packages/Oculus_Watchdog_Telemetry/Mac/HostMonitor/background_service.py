#!/usr/bin/env python3
import argparse
import fcntl
import grp
import os
from pathlib import Path
import plistlib
import pwd
import subprocess
import sys
import tempfile
import time

from mac_host_monitor import DEFAULT_STATE, VERSION, get_key, json_object, private_directory, read_config

LABEL = "org.sfmoma.oculus.hostmonitor"
APP = Path("/Library/Application Support/SFMoMAOculus/HostMonitor")
PLIST = Path("/Library/LaunchDaemons") / (LABEL + ".plist")
ROOT = Path(__file__).resolve().parent


def job_definition(username, groupname, runtime, app, state):
    if username == "root":
        raise ValueError("The observation service must run as a non-root user")
    return {
        "Label": LABEL,
        "ProgramArguments": [str(runtime), "-u", str(app / "mac_host_monitor.py"),
                             "--config", str(app / "monitor.config.json"),
                             "--state-dir", str(state), "--quiet"],
        "UserName": username, "GroupName": groupname,
        "WorkingDirectory": str(app), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 15,
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
    }


def admin(arguments, check=True, capture=False):
    return subprocess.run(["/usr/bin/sudo", "-n"] + [str(item) for item in arguments],
                          check=check, capture_output=capture, text=True)


def job_loaded():
    return admin(["/bin/launchctl", "print", "system/" + LABEL], check=False, capture=True).returncode == 0


def trusted_root_directory(path):
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("System installation directory must be a real directory: " + str(path))
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("System installation directory must be root-owned and not group/world writable: " + str(path))


def wait_for_monitor(state, started):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            snapshot = json_object((state / "status.json").read_text(encoding="utf-8"))
            if (snapshot["version"] == VERSION and snapshot["process_state"] == "running"
                    and started <= snapshot["updated_unix"] <= time.time()):
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.25)
    raise RuntimeError("launchd registered the host monitor, but no fresh status arrived. Check Show-Status.command and the launchd job log")


def main():
    parser = argparse.ArgumentParser(description="Install or remove the Mac host monitor launchd service.")
    parser.add_argument("action", choices=("install", "remove"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("Run background-service management on the Mac mini")
    if os.geteuid() == 0:
        raise RuntimeError("Run this launcher as sfmoma without sudo; it requests admin rights only for installation")
    account = pwd.getpwuid(os.getuid())
    if account.pw_name != "sfmoma":
        raise RuntimeError("This package targets the collected sfmoma account; run it from that account")
    if PLIST.is_symlink():
        raise RuntimeError("The installed job file must not be a symlink")
    subprocess.run(["/usr/bin/sudo", "-v"], check=True)
    if args.action == "remove":
        if PLIST.exists():
            with PLIST.open("rb") as file:
                existing = plistlib.load(file)
            if (existing.get("Label") != LABEL or existing.get("UserName") != account.pw_name or
                    str(APP / "mac_host_monitor.py") not in existing.get("ProgramArguments", [])):
                raise RuntimeError("Existing job does not match this package; no file removed")
            if job_loaded():
                admin(["/bin/launchctl", "bootout", "system/" + LABEL])
                deadline = time.monotonic() + 15
                while job_loaded():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("launchd is still unloading the host monitor. Rerun Remove-Background.command once it clears")
                    time.sleep(0.2)
            admin(["/bin/rm", "-f", str(PLIST)])
        elif job_loaded():
            raise RuntimeError("A loaded job has no matching installed plist; no service stopped")
        print("Background host monitor removed. State, shared key, logs and installed app files were preserved.")
        return

    read_config(ROOT / "monitor.config.json")
    if job_loaded():
        raise RuntimeError("Background monitor is already installed/running; remove it before installing an update")
    if PLIST.exists():
        raise RuntimeError("A job file already exists. Run Remove-Background.command before reinstalling")
    state = private_directory(DEFAULT_STATE)
    lock_path = state / "monitor.lock"
    if lock_path.is_symlink():
        raise RuntimeError("The monitor lock must not be a symlink")
    with lock_path.open("a+", encoding="ascii") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Stop the foreground monitor with Ctrl+C before installing background startup") from exc
        get_key(state)
        definition = job_definition(account.pw_name, grp.getgrgid(account.pw_gid).gr_name,
                                    Path(sys.executable).resolve(), APP, state)
        with tempfile.TemporaryDirectory(prefix="oculus-install-") as temporary:
            staged = Path(temporary) / "hostmonitor.plist"
            staged.write_bytes(plistlib.dumps(definition))
            trusted_root_directory(Path("/Library/Application Support"))
            trusted_root_directory(PLIST.parent)
            for folder in (APP.parent, APP):
                if not folder.exists() and not folder.is_symlink():
                    admin(["/usr/bin/install", "-d", "-o", "root", "-g", "wheel", "-m", "0755", str(folder)])
                trusted_root_directory(folder)
            for name in ("mac_host_monitor.py", "pikvm.py", "ups_snmp.py", "ups_automation.py",
                         "windows_power.py", "monitor.config.json"):
                if (APP / name).is_symlink():
                    raise RuntimeError("Installed app targets must not be symlinks")
                admin(["/usr/bin/install", "-o", "root", "-g", "wheel", "-m", "0644", str(ROOT / name), str(APP / name)])
            candidate = APP / "hostmonitor.plist.pending"
            if candidate.is_symlink():
                raise RuntimeError("Staged launchd target must not be a symlink")
            admin(["/usr/bin/install", "-o", "root", "-g", "wheel", "-m", "0644", str(staged), str(candidate)])
            with candidate.open("rb") as file:
                if plistlib.load(file) != definition:
                    raise RuntimeError("Staged launchd definition changed; no job installed")
            admin(["/usr/bin/plutil", "-lint", str(candidate)])
            admin(["/usr/bin/install", "-o", "root", "-g", "wheel", "-m", "0644", str(candidate), str(PLIST)])
            admin(["/bin/rm", "-f", str(candidate)])
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    started = time.time()
    try:
        admin(["/bin/launchctl", "bootstrap", "system", str(PLIST)])
    except subprocess.CalledProcessError:
        admin(["/bin/launchctl", "bootout", "system/" + LABEL], check=False, capture=True)
        admin(["/bin/rm", "-f", str(PLIST)])
        raise RuntimeError("launchd bootstrap failed; the job was rolled back, and logs/state were preserved")
    wait_for_monitor(state, started)
    print("Background host monitor installed for sfmoma and publishing fresh status. It starts at boot and survives logout.")
    print("Use Show-Status.command to inspect PiKVM and recovery mode.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print("Background service: " + str(exc), file=sys.stderr)
        sys.exit(1)
