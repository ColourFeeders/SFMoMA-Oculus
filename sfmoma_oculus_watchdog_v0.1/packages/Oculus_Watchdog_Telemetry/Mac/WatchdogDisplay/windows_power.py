import os
import stat
import subprocess

WINDOWS_ACCOUNT = "oculuspower"


class PowerError(Exception):
    pass


def private_file(path):
    try:
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
    except OSError:
        return False


class WindowsPower:
    def __init__(self, app, host, source):
        self.key = app / "windows-power"
        self.known = app / "windows-known-hosts"
        self.host = host
        self.source = source

    def configured(self):
        return private_file(self.key) and private_file(self.known)

    def send(self, action):
        if not self.configured():
            raise PowerError("Windows power access needs setup.")
        args = [
            "/usr/bin/ssh", "-F", "/dev/null", "-T", "-b", self.source,
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", f'UserKnownHostsFile="{self.known}"',
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ClearAllForwardings=yes",
            "-o", "ConnectTimeout=4", "-i", str(self.key), f"{WINDOWS_ACCOUNT}@{self.host}", action,
        ]
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=12)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if action == "status":
                raise PowerError("Windows SSH status check timed out or could not start. No power request was sent.") from exc
            raise PowerError("Windows did not confirm the power request. Check the server before retrying.") from exc
        expected = "OCULUS_POWER_READY" if action == "status" else "OCULUS_POWER_ACCEPTED"
        if expected in result.stdout.splitlines() and (result.returncode == 0 or
                (action != "status" and result.returncode == 255)):
            return
        if action == "status":
            if result.returncode == 13:
                raise PowerError("Windows SSH account lacks Force shutdown from a remote system. No power request was sent.")
            if result.returncode == 11:
                raise PowerError("Windows SSH account lacks Shut down the system. No power request was sent.")
            raise PowerError(f"Windows SSH status check failed (exit {result.returncode}). No power request was sent.")
        raise PowerError(f"Windows did not confirm the power request (SSH exit {result.returncode}). Check the server before retrying.")
