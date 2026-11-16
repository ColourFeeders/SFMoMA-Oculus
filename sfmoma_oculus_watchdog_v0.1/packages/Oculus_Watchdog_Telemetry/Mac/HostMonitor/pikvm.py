import base64
import getpass
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import stat
import time

POLL_SECONDS = 5
ATX_MAX_AGE = 10
OUTAGE_SECONDS = 60
BOOT_GRACE_SECONDS = 300
RETURN_STABILITY_SECONDS = 20
RESTART_GUARD_TIMEOUT = 15
RESET_INTERVAL_SECONDS = 5 * 60
MAX_RESPONSE = 4096


class PiKVMError(Exception):
    pass


def credentials(username, password, fingerprint):
    fingerprint = fingerprint.rsplit("=", 1)[-1].strip().replace(":", "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise PiKVMError("invalid_certificate_fingerprint")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username) or not 1 <= len(password) <= 256:
        raise PiKVMError("invalid_web_credentials")
    return {"username": username, "password": password, "fingerprint": fingerprint}


class PiKVM:
    def __init__(self, ip, source_ip, path, port=443):
        self.ip = ip
        self.source_ip = source_ip
        self.path = path
        self.port = port

    def read_credentials(self):
        try:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_RESPONSE):
                raise PiKVMError("credentials_permissions")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return credentials(value["username"], value["password"], value["fingerprint"])
        except FileNotFoundError as exc:
            raise PiKVMError("credentials_missing") from exc
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise PiKVMError("credentials_invalid") from exc

    def request(self, method, path, auth, before_send=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            self.ip, self.port, timeout=2, context=context, source_address=(self.source_ip, 0)
        )
        try:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, auth["fingerprint"]):
                raise PiKVMError("certificate_mismatch")
            token = base64.b64encode((auth["username"] + ":" + auth["password"]).encode("utf-8")).decode("ascii")
            if before_send is not None:
                before_send()
            connection.request(method, path, headers={
                "Authorization": "Basic " + token, "Accept": "application/json", "Connection": "close",
            })
            with connection.getresponse() as response:
                if response.status in (401, 403):
                    raise PiKVMError("web_auth_rejected")
                if response.status != 200:
                    raise PiKVMError("http_" + str(response.status))
                data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise PiKVMError("response_too_large")
            obj = json.loads(data)
            if type(obj) is not dict or obj.get("ok") is not True or type(obj.get("result")) is not dict:
                raise PiKVMError("invalid_api_result")
            return obj["result"]
        except TimeoutError as exc:
            raise PiKVMError("timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise PiKVMError("connection_or_tls_error") from exc
        except (ValueError, RecursionError) as exc:
            raise PiKVMError("invalid_api_result") from exc
        finally:
            connection.close()

    def probe(self, auth=None):
        try:
            value = self.request("GET", "/api/atx", self.read_credentials() if auth is None else auth)
            enabled, busy, power = value["enabled"], value["busy"], value["leds"]["power"]
            if any(type(item) is not bool for item in (enabled, busy, power)):
                raise PiKVMError("invalid_atx_fields")
            return dict(state="reachable", enabled=enabled, busy=busy, power_led=power)
        except PiKVMError as exc:
            return dict(state="unavailable", reason=str(exc), enabled=None, busy=None, power_led=None)
        except (KeyError, TypeError):
            return dict(state="unavailable", reason="invalid_atx_fields", enabled=None, busy=None, power_led=None)

    def reset(self, before_send):
        self.request("POST", "/api/atx/power?action=reset_hard&wait=1", self.read_credentials(), before_send)


def prompt_credentials():
    fingerprint = input("PiKVM SHA-256 certificate fingerprint from its local console: ")
    username = input("PiKVM web username [admin]: ").strip() or "admin"
    password = getpass.getpass("PiKVM web password: ")
    return credentials(username, password, fingerprint)


def atx_ready(atx):
    return (atx.get("state") == "reachable" and atx.get("enabled") is True
            and atx.get("busy") is False and atx.get("power_led") is True)


class RecoveryPolicy:
    def __init__(self, mode, attempted_at=None, now=None):
        self.mode = mode
        self.power_led = None
        self.healthy_source = None
        self.boot_grace_until = None
        self.previous_boot_source = None
        self.clear_incident()
        if attempted_at is not None and mode == "armed":
            self.last_contact = now
            self.note_reset(attempted_at, "result_unknown")

    def clear_incident(self):
        self.last_contact = None
        self.next_reset_at = None
        self.last_reset_result = None
        self.preflight_retry_at = 0
        self.preflight_failures = 0
        self.preflight_reason = None
        self.same_source_healthy_since = None

    def defer_reset(self, now, reason):
        delay = min(30, 2 ** min(5, self.preflight_failures + 1))
        self.preflight_retry_at = now + delay
        self.preflight_failures += 1
        self.preflight_reason = reason

    def note_reset(self, now, result):
        self.next_reset_at = now + RESET_INTERVAL_SECONDS
        self.last_reset_result = result
        self.preflight_failures = 0
        self.preflight_retry_at = 0
        self.preflight_reason = None

    def note_restart(self, now):
        self.boot_grace_until = now + BOOT_GRACE_SECONDS
        self.previous_boot_source = self.healthy_source
        self.clear_incident()

    def evaluate(self, now, mode, maintenance, host, atx, atx_age, guard_last_ready):
        if (atx_age <= ATX_MAX_AGE and atx.get("state") == "reachable"
                and atx.get("enabled") is True and atx.get("busy") is False):
            power_led = atx.get("power_led")
            if self.power_led is False and power_led is True and self.next_reset_at is None:
                self.note_restart(now)
            self.power_led = power_led

        if mode != self.mode:
            self.clear_incident()
            self.mode = mode
        if maintenance or mode == "disabled":
            self.clear_incident()
            return "maintenance" if maintenance else "disabled"

        host_state = host["state"]
        source = (host.get("boot_id"), host.get("stream_id"))
        guard_ready = now - guard_last_ready < RESTART_GUARD_TIMEOUT
        progressing = host_state == "windows_heartbeat_progressing"
        if self.boot_grace_until is not None:
            contact_restored = progressing and guard_ready
            boot_complete = contact_restored and source != self.previous_boot_source
            held_since = self.boot_grace_until - BOOT_GRACE_SECONDS
            if (contact_restored and source == self.previous_boot_source and guard_last_ready >= held_since
                    and now - host["age_seconds"] >= held_since):
                if self.same_source_healthy_since is None:
                    self.same_source_healthy_since = now
                boot_complete = now - self.same_source_healthy_since >= RETURN_STABILITY_SECONDS
            else:
                self.same_source_healthy_since = None
            if not boot_complete:
                return "windows_boot_grace" if now < self.boot_grace_until else "windows_boot_delayed"
            self.boot_grace_until = None

        if progressing and guard_ready:
            self.clear_incident()
            self.last_contact = max(now - host["age_seconds"], guard_last_ready)
            self.healthy_source = source
            return "observing"
        if host_state in ("windows_stopping", "windows_restarting", "windows_maintenance",
                          "conflicting_host_senders"):
            self.clear_incident()
            return "host_recovery_inhibited"
        if self.last_contact is None:
            return "waiting_for_restart_guard" if progressing else "waiting_for_healthy_baseline"

        self.last_contact = max(self.last_contact, guard_last_ready)
        if host.get("age_seconds") is not None:
            self.last_contact = max(self.last_contact, now - host["age_seconds"])
        if host_state not in ("heartbeat_stale", "awaiting_windows_heartbeat"):
            return "waiting_for_restart_guard" if progressing else "waiting_for_windows_heartbeat"
        if guard_ready:
            return "windows_guard_reporting"
        if now - self.last_contact < OUTAGE_SECONDS:
            return "confirming_outage"
        if atx_age > ATX_MAX_AGE or not atx_ready(atx):
            return "atx_unavailable_or_server_off"
        if self.next_reset_at is not None and now < self.next_reset_at:
            return "reset_interval_wait"
        if mode == "armed" and now < self.preflight_retry_at:
            return "reset_preflight_wait"
        return "ready_to_reset" if mode == "armed" else "would_reset"
