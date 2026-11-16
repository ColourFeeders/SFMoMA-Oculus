#!/usr/bin/env python3
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from prepare_deployment import render

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {".git", "__pycache__", ".venv", "deploy", "private", "backups", "diagnostics-output", "network-exports"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".pfx", ".p12", ".toe", ".tox", ".mad", ".mov", ".mp4", ".hap", ".bin", ".zip", ".exe", ".dll"}
TELEMETRY = Path("packages/Oculus_Watchdog_Telemetry")
HEARTBEAT = "Send-OculusHostHeartbeat.ps1"
POWER_HANDLER = "Invoke-OculusPower.ps1"
TD_A_DAT = "Oculus_TD_A_Execute_DAT.py"
DUPLICATE_SOURCES = (
    (TELEMETRY / "Windows" / HEARTBEAT,
     TELEMETRY / "Mac/HostMonitor/Windows" / HEARTBEAT,
     Path("packages/Oculus_Host_Heartbeat_Startup") / HEARTBEAT),
    (Path("packages/Oculus_Windows_SSH_Setup") / POWER_HANDLER,
     TELEMETRY / "Mac/WatchdogDisplay/Windows" / POWER_HANDLER),
    (Path("touchdesigner") / TD_A_DAT, TELEMETRY / "TouchDesigner" / TD_A_DAT),
    (TELEMETRY / "Mac/HostMonitor/windows_power.py", TELEMETRY / "Mac/WatchdogDisplay/windows_power.py"),
)


def main():
    counts = {"python": 0, "json": 0, "shell": 0, "javascript": 0}
    bash = shutil.which("bash")
    node = shutil.which("node")
    if not bash or not shutil.which("openssl"):
        raise RuntimeError("Bash and OpenSSL are required for full local validation.")
    for path in ROOT.rglob("*"):
        if set(path.relative_to(ROOT).parts) & EXCLUDE:
            continue
        if path.is_symlink():
            raise ValueError(f"Linked source found: {path.relative_to(ROOT)}")
        if not path.is_file():
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"Excluded artifact found: {path.relative_to(ROOT)}")
        if path.name in {"site.local.json", "guard.config", "authorized_keys", "windows-known-hosts",
                         "ups-snmp.credentials.json", "ups-power-cycle.json"}:
            raise ValueError(f"Local configuration found: {path.relative_to(ROOT)}")
        if path.suffix.lower() == ".pdf":
            continue
        data = path.read_text(encoding="utf-8")
        if re.search(r"-----BEGIN (?:OPENSSH |RSA |EC |ENCRYPTED )?PRIVATE KEY-----", data):
            raise ValueError(f"Private key found: {path.relative_to(ROOT)}")
        if re.search(r"\b10\.1\.100\.\d+\b", data):
            raise ValueError(f"Unredacted site address found: {path.relative_to(ROOT)}")
        if path.suffix == ".py":
            ast.parse(data, filename=str(path))
            counts["python"] += 1
        elif path.suffix == ".json":
            json.loads(data)
            counts["json"] += 1
        elif path.suffix in {".sh", ".command"}:
            subprocess.run([bash, "-n", str(path)], check=True)
            counts["shell"] += 1
        elif path.suffix == ".js" and node:
            subprocess.run([node, "--check", str(path)], check=True)
            counts["javascript"] += 1
    manifest = json.loads((ROOT / "docs/source-manifest.json").read_text())
    for row in manifest["files"]:
        path = ROOT / row["path"]
        data = path.read_text(encoding="utf-8").encode("utf-8")
        if hashlib.sha256(data).hexdigest() != row["packaged_sha256"]:
            raise ValueError(f"Source hash mismatch: {row['path']}; update provenance for intentional changes.")
    for canonical, *copies in DUPLICATE_SOURCES:
        original = (ROOT / canonical).read_text(encoding="utf-8")
        for copy in copies:
            if original != (ROOT / copy).read_text(encoding="utf-8"):
                raise ValueError(f"Source copy differs: {copy} vs. {canonical}")
    monitor = ROOT / TELEMETRY / "Mac/HostMonitor"
    subprocess.run([sys.executable, "-B", "-m", "unittest", "-v", "test_monitor", "test_pikvm", "test_ups"],
                   cwd=monitor, check=True)
    with tempfile.TemporaryDirectory(prefix="oculus-package-test-") as temporary:
        target = Path(temporary) / "deploy"
        settings = {"mac_ip": "198.51.100.10", "windows_ip": "198.51.100.11", "pikvm_ip": "198.51.100.12"}
        render(settings, target)
        rendered = json.loads((target / TELEMETRY / "Mac/HostMonitor/monitor.config.json").read_text())
        assert rendered["mac_bind_ip"] == settings["mac_ip"]
        assert rendered["windows_ip"] == settings["windows_ip"]
        assert rendered["pikvm_ip"] == settings["pikvm_ip"]
        try:
            render(settings, target)
        except FileExistsError:
            pass
        else:
            raise AssertionError("Deployment renderer overwrote an existing directory.")
        assert (target / "packages/Oculus_Watchdog_Observer/observer.config.json").is_file()
    print("PASS:", counts, ";", len(manifest["files"]), "source hashes; source-copy checks; host-monitor regressions; local rendering and overwrite protection.")
    print("Not exercised here: Windows PowerShell/C# runtime, macOS installation, TouchDesigner, PiKVM hardware or projected output.")


if __name__ == "__main__":
    main()
