#!/usr/bin/env python3
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import time

import pikvm
from ups_automation import UPSAutomation
from ups_snmp import UPSProbe, prompt_credentials
from windows_power import WindowsPower

VERSION = "0.2.6"
MAX_PACKET = 4096
MAX_SOURCES = 16
SHOW_TIMEOUT = 6
DEFAULT_STATE = Path.home() / "Library/Application Support/SFMoMAOculus/HostMonitor"


def application_status(value, sent_unix):
    if type(value) is not dict:
        return {"state": "unreadable"}
    state = value.get("state")
    if state in ("missing", "stale", "unreadable"):
        return {"state": state}
    sampled_ms = value.get("sampled_ms")
    if state != "reporting" or type(sampled_ms) is not int:
        return {"state": "unreadable"}
    age = sent_unix - sampled_ms / 1000 if 0 < sampled_ms < 2**53 else math.inf
    if not -1 <= age < SHOW_TIMEOUT:
        return {"state": "stale"}

    roles = value.get("roles")
    mapper = value.get("madmapper")
    if type(roles) is not dict or mapper not in ("running", "not_running", "unknown"):
        return {"state": "unreadable"}
    result = {"state": "reporting", "age_seconds": max(0, age), "madmapper": mapper, "roles": {}}
    for name in ("TD_A", "TD_B"):
        role = roles.get(name)
        if type(role) is not dict:
            continue
        info = {key: role[key] for key in ("state", "process_match", "fault", "detail")
                if type(role.get(key)) is str and len(role[key]) <= 180}
        age = role.get("age_seconds")
        if type(age) in (int, float) and 0 <= age < 120:
            info["age_seconds"] = age
        result["roles"][name] = info
    for key in ("accepted_packets", "rejected_packets"):
        count = value.get(key)
        if type(count) is int and 0 <= count < 2**53:
            result[key] = count
    return result


def json_object(text):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("DUPLICATE_JSON_KEY")
            obj[key] = value
        return obj
    try:
        obj = json.loads(text, object_pairs_hook=pairs,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError("INVALID_JSON_NUMBER")))
    except RecursionError as exc:
        raise ValueError("JSON_TOO_DEEP") from exc
    if type(obj) is not dict:
        raise ValueError("JSON_OBJECT_REQUIRED")
    return obj


def read_config(path):
    cfg = json_object(Path(path).read_text(encoding="utf-8"))
    required = {"mode", "mac_bind_ip", "windows_ip", "windows_host", "heartbeat_port",
                "heartbeat_timeout_seconds", "stability_seconds", "max_clock_skew_seconds",
                "source_retention_seconds", "ping_interval_seconds", "ping_timeout_seconds",
                "summary_interval_seconds"}
    if not required <= set(cfg) <= required | {"pikvm_ip", "allow_network_only_reset"} or cfg["mode"] != "observe":
        raise ValueError("Unsupported monitor configuration")
    cfg.pop("allow_network_only_reset", None)
    for name in ("mac_bind_ip", "windows_ip", "pikvm_ip"):
        if name == "pikvm_ip" and name not in cfg:
            continue
        if type(cfg[name]) is not str or ipaddress.ip_address(cfg[name]).version != 4:
            raise ValueError("An explicit IPv4 address is required: " + name)
    if cfg.get("pikvm_ip") in (cfg["windows_ip"], cfg["mac_bind_ip"]):
        raise ValueError("The PiKVM must have its own address")
    if type(cfg["windows_host"]) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", cfg["windows_host"]):
        raise ValueError("Invalid Windows host name")
    limits = {
        "heartbeat_port": (1024, 65535), "heartbeat_timeout_seconds": (3, 120),
        "stability_seconds": (0, 120), "max_clock_skew_seconds": (5, 300),
        "source_retention_seconds": (30, 3600), "ping_interval_seconds": (3, 300),
        "ping_timeout_seconds": (1, 5), "summary_interval_seconds": (5, 3600),
    }
    for name, (low, high) in limits.items():
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise ValueError("Invalid integer/range: " + name)
    if cfg["source_retention_seconds"] <= cfg["heartbeat_timeout_seconds"]:
        raise ValueError("Source retention must exceed heartbeat timeout")
    return cfg


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("State directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ValueError("State directory must belong to the current service user")
    path.chmod(0o700)
    return path


def atomic_json(path, data, durable=False):
    path = Path(path)
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            os.chmod(str(temporary), 0o600)
            json.dump(data, file, ensure_ascii=True, indent=2, allow_nan=False)
            file.write("\n")
            if durable:
                file.flush()
                os.fsync(file.fileno())
        os.replace(str(temporary), str(path))
        if durable:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def get_key(state_dir):
    path = state_dir / "host-heartbeat.key"
    if path.is_symlink():
        raise ValueError("Shared key must not be a symlink")
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="ascii") as file:
                file.write(secrets.token_hex(32) + "\n")
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError("The local host-heartbeat.key must contain exactly 64 hex characters")
    path.chmod(0o600)
    return bytes.fromhex(value)


def decode_heartbeat(raw, peer_ip, key, cfg, wall_time):
    if peer_ip != cfg["windows_ip"]:
        raise ValueError("WRONG_SOURCE_IP")
    if not 0 < len(raw) <= MAX_PACKET:
        raise ValueError("PACKET_SIZE")
    try:
        envelope = json_object(raw.decode("utf-8"))
        if set(envelope) != {"payload_b64", "hmac_sha256"}:
            raise ValueError("ENVELOPE_FIELDS")
        encoded, signature = envelope["payload_b64"], envelope["hmac_sha256"]
        if type(encoded) is not str or type(signature) is not str or not re.fullmatch(r"[a-f0-9]{64}", signature):
            raise ValueError("ENVELOPE_TYPES")
        payload = base64.b64decode(encoded, validate=True)
    except (UnicodeError, TypeError, KeyError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("MALFORMED_ENVELOPE") from exc
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("BAD_SIGNATURE")
    try:
        packet = json_object(payload.decode("utf-8"))
        schema = packet.get("schema")
        if schema not in ("oculus.windows.host", "oculus.windows.lifecycle") or type(packet.get("version")) is not int or packet["version"] != 1:
            raise ValueError("PAYLOAD_SCHEMA")
        lifecycle = schema == "oculus.windows.lifecycle"
        required = {"schema", "version", "host", "stream_id", "sequence", "sent_unix", "phase"}
        if not lifecycle:
            required.add("boot_id")
        optional = set() if lifecycle else {"show"}
        if not required <= set(packet) <= required | optional:
            raise ValueError("PAYLOAD_FIELDS")
        if type(packet["host"]) is not str or packet["host"].upper() != cfg["windows_host"].upper():
            raise ValueError("HOST_MISMATCH")
        if type(packet["stream_id"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", packet["stream_id"]):
            raise ValueError("STREAM_ID")
        if not lifecycle and (type(packet["boot_id"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,64}", packet["boot_id"])):
            raise ValueError("BOOT_ID")
        if type(packet["sequence"]) is not int or not 0 < packet["sequence"] < 2 ** 63:
            raise ValueError("SEQUENCE")
        if type(packet["sent_unix"]) is not int or abs(wall_time - packet["sent_unix"]) > cfg["max_clock_skew_seconds"]:
            raise ValueError("CLOCK_OR_OLD_PACKET")
        phases = ("ready", "stopping") if lifecycle else ("running", "maintenance", "starting", "restarting", "stopping")
        if packet["phase"] not in phases:
            raise ValueError("PHASE")
    except (UnicodeError, TypeError, KeyError, json.JSONDecodeError, ValueError) as exc:
        reason = str(exc)
        known = {"PAYLOAD_FIELDS", "PAYLOAD_SCHEMA", "HOST_MISMATCH", "STREAM_ID",
                 "BOOT_ID", "SEQUENCE", "CLOCK_OR_OLD_PACKET", "PHASE"}
        raise ValueError(reason if reason in known else "INVALID_PAYLOAD") from exc
    if "show" in packet:
        packet["show"] = application_status(packet["show"], packet["sent_unix"])
    return packet


class HostTracker:
    def __init__(self, cfg):
        self.cfg, self.sources = cfg, {}
        self.ever_seen, self.accepted = False, 0
        self.rejected = Counter()
        self.guard = None
        self.guard_last_ready = -math.inf

    def accept_lifecycle(self, packet, now):
        previous = self.guard
        new_stream = previous is not None and packet["stream_id"] != previous["stream_id"]
        if previous is not None:
            if new_stream:
                if packet["sent_unix"] <= previous["sent_unix"]:
                    raise ValueError("LIFECYCLE_OLD_STREAM")
            elif (packet["sequence"] <= previous["sequence"]
                  or packet["sent_unix"] < previous["sent_unix"]
                  or (previous["phase"] == "stopping" and packet["phase"] == "ready")):
                raise ValueError("LIFECYCLE_SEQUENCE")
        self.guard = dict(packet, received_at=now)
        self.guard_last_ready = now if packet["phase"] == "ready" else -math.inf
        return packet["phase"] == "stopping" and (new_stream or previous is None or previous["phase"] != "stopping")

    def guard_ready(self, now):
        return (self.guard is not None and self.guard["phase"] == "ready"
                and now - self.guard["received_at"] < pikvm.RESTART_GUARD_TIMEOUT)

    def prune(self, now):
        self.sources = {key: value for key, value in self.sources.items()
                        if now - value["last"] < self.cfg["source_retention_seconds"]}

    def accept(self, packet, now):
        self.prune(now)
        identity = (packet["boot_id"], packet["stream_id"])
        source = self.sources.get(identity)
        if source and packet["sequence"] <= source["packet"]["sequence"]:
            raise ValueError("SEQUENCE_NOT_ADVANCING")
        if source is None:
            if len(self.sources) >= MAX_SOURCES:
                raise ValueError("SOURCE_LIMIT")
            source = {"last": now, "good_since": None, "running_packets": 0, "packet": packet}
            self.sources[identity] = source
        if now - source["last"] >= self.cfg["heartbeat_timeout_seconds"]:
            source["good_since"] = None
            source["running_packets"] = 0
        source.update(last=now, packet=packet)
        if packet["phase"] == "running":
            source["running_packets"] += 1
            if source["good_since"] is None:
                source["good_since"] = now
        else:
            source["good_since"] = None
            source["running_packets"] = 0
        fresh = self.fresh(now)
        if len(fresh) > 1:
            for item in fresh:
                item["good_since"] = None
                item["running_packets"] = 0
        self.accepted += 1
        self.ever_seen = True

    def fresh(self, now):
        return [item for item in self.sources.values()
                if now - item["last"] < self.cfg["heartbeat_timeout_seconds"]]

    def status(self, now):
        self.prune(now)
        fresh = self.fresh(now)
        result = {"state": "awaiting_windows_heartbeat", "fresh_sources": len(fresh),
                  "age_seconds": None, "sequence": None, "phase": None}
        if len(fresh) > 1:
            result["state"] = "conflicting_host_senders"
        elif not fresh:
            if self.ever_seen:
                result["state"] = "heartbeat_stale"
        else:
            source = fresh[0]
            result.update(age_seconds=round(now - source["last"], 2),
                          sequence=source["packet"]["sequence"], phase=source["packet"]["phase"],
                          boot_id=source["packet"]["boot_id"], stream_id=source["packet"]["stream_id"])
            if result["phase"] != "running":
                result["state"] = "windows_" + result["phase"]
            elif (source["running_packets"] < 2 or source["good_since"] is None
                  or now - source["good_since"] < self.cfg["stability_seconds"]):
                result["state"] = "validating_host_heartbeat"
            else:
                result["state"] = "windows_heartbeat_progressing"
        return result

    def show_status(self, now):
        fresh = self.fresh(now)
        if len(fresh) != 1:
            return {"state": "windows_heartbeat_unavailable"}
        source = fresh[0]
        show = source["packet"].get("show", {"state": "waiting"})
        if show["state"] != "reporting":
            return show
        packet_age = now - source["last"]
        age = packet_age + show["age_seconds"]
        if age >= SHOW_TIMEOUT:
            return {"state": "stale"}
        result = dict(show, age_seconds=round(age, 1), roles={})
        for name, role in show["roles"].items():
            result["roles"][name] = dict(role)
            if "age_seconds" in role:
                result["roles"][name]["age_seconds"] = round(role["age_seconds"] + age, 1)
        return result


def probe_network(cfg):
    if sys.platform != "darwin":
        return {"state": "not_checked", "reason": "Native ping requires macOS"}
    command = ["/sbin/ping", "-n", "-c", "1", "-W", "1000", "-t", "2",
               "-S", cfg["mac_bind_ip"], cfg["windows_ip"]]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=cfg["ping_timeout_seconds"], check=False)
        if result.returncode == 0:
            return {"state": "icmp_reply", "reason": "Network response only; not proof of Windows/show health"}
        if result.returncode == 2:
            return {"state": "no_icmp_reply", "reason": "Host state unknown; ICMP may be blocked"}
        return {"state": "probe_error", "reason": "Local ping exit status " + str(result.returncode)}
    except subprocess.TimeoutExpired:
        return {"state": "probe_timeout", "reason": "Bounded ping query timed out"}
    except OSError:
        return {"state": "probe_error", "reason": "Could not execute native ping"}


def maintenance_on(directory):
    path = directory / "maintenance.json"
    try:
        value = json_object(path.read_text(encoding="utf-8"))["enabled"]
        if type(value) is not bool:
            raise ValueError("Maintenance flag must be boolean")
        return value
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError):
        return True


def recovery_mode(directory, api):
    if api is None:
        return "disabled"
    try:
        flag = json_object((directory / "pikvm-recovery.json").read_text(encoding="utf-8"))
        if set(flag) == {"armed"} and flag["armed"] is True:
            return "armed"
    except FileNotFoundError:
        return "armed"
    except (OSError, ValueError):
        pass
    return "dry_run"


class ResetBudget:
    def __init__(self, directory):
        self.path = directory / "pikvm-reset-budget.json"

    def read(self):
        try:
            data = json_object(self.path.read_text(encoding="utf-8"))
            last = data["attempted_unix"]
            active = data.get("active", False)
            if type(last) not in (int, float) or not 0 < last < 2 ** 53 or type(active) is not bool:
                raise ValueError("invalid reset budget")
            return last, active
        except FileNotFoundError:
            return None, False
        except (OSError, ValueError, KeyError) as exc:
            raise pikvm.PiKVMError("reset_budget_unreadable") from exc

    def write(self, last, active):
        try:
            atomic_json(self.path, {"attempted_unix": last, "active": active}, durable=True)
        except OSError as exc:
            raise pikvm.PiKVMError("reset_budget_write_failed") from exc

    def reserve(self, now):
        last, active = self.read()
        if active and now - last < pikvm.RESET_INTERVAL_SECONDS:
            raise pikvm.PiKVMError("reset_cooldown")
        self.write(now, True)

    def finish(self):
        last, active = self.read()
        if active:
            self.write(last, False)

    def outstanding_attempt(self, wall, monotonic):
        last, active = self.read()
        if not active:
            return None
        elapsed = max(0, wall - last)
        return monotonic - elapsed


def make_logger(directory, filename="events.jsonl"):
    logger = logging.getLogger("oculus." + filename)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(str(directory / filename), maxBytes=2 * 1024 * 1024,
                                  backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def run_monitor(cfg, directory, quiet=False):
    directory = private_directory(directory)
    lock_path = directory / "monitor.lock"
    if lock_path.is_symlink():
        raise ValueError("Monitor lock must not be a symlink")
    with lock_path.open("a+", encoding="ascii") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another host monitor is already using this state directory") from exc
        key = get_key(directory)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        logger = make_logger(directory)
        show_logger = make_logger(directory, "show-events.jsonl")
        tracker = HostTracker(cfg)
        api = pikvm.PiKVM(cfg["pikvm_ip"], cfg["mac_bind_ip"], directory / "pikvm.credentials.json") if "pikvm_ip" in cfg else None
        windows = WindowsPower(directory.parent / "WatchdogDisplay", cfg["windows_ip"], cfg["mac_bind_ip"])
        ups = UPSAutomation(directory, windows, api, atomic_json, lambda: maintenance_on(directory))
        worker = ThreadPoolExecutor(max_workers=3 if api else 2)
        pending_network = pending_atx = None
        atx_submitted = atx_sampled = -math.inf
        atx = {"state": "pending" if api else "disabled"}
        budget = ResetBudget(directory)
        attempt_active = False
        started = time.monotonic()
        try:
            attempted_at = budget.outstanding_attempt(time.time(), started) if api else None
            attempt_active = attempted_at is not None
        except pikvm.PiKVMError as exc:
            attempted_at = None
            logger.warning(json.dumps({"at_unix": time.time(), "event": "recovery_state_error", "data": str(exc)}))
        policy = pikvm.RecoveryPolicy(recovery_mode(directory, api), attempted_at, started)
        network = {"state": "not_checked", "reason": "Probe pending"}
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setblocking(False)
        stop = [False]
        previous_handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda number, frame: stop.__setitem__(0, True))
        last_probe = last_status = last_summary = -math.inf
        previous = None
        previous_show = {}
        snapshot = {}
        def event(name, data, output=logger):
            output.info(json.dumps({"at_unix": time.time(), "event": name, "data": data}, ensure_ascii=True))
        try:
            udp.bind((cfg["mac_bind_ip"], cfg["heartbeat_port"]))
            if not quiet:
                print("Oculus Mac host monitor", flush=True)
                print("Windows: " + cfg["windows_ip"] + " | listen: " + cfg["mac_bind_ip"] + ":" + str(cfg["heartbeat_port"]), flush=True)
                print("Status/logs: " + str(directory), flush=True)
            event("monitor_started", {"mode": "observe", "windows_ip": cfg["windows_ip"]})
            while not stop[0]:
                if select.select([udp], [], [], 0.2)[0]:
                    for _ in range(64):
                        try:
                            raw, peer = udp.recvfrom(MAX_PACKET + 1)
                        except BlockingIOError:
                            break
                        try:
                            packet = decode_heartbeat(raw, peer[0], key, cfg, time.time())
                            received_at = time.monotonic()
                            if packet["schema"] == "oculus.windows.lifecycle":
                                if tracker.accept_lifecycle(packet, received_at):
                                    policy.note_restart(received_at)
                                    event("windows_restart_grace_started", {"phase": packet["phase"]})
                            else:
                                tracker.accept(packet, received_at)
                        except ValueError as exc:
                            reason = str(exc)
                            tracker.rejected[reason if re.fullmatch(r"[A-Z_]{1,40}", reason) else "INVALID_PACKET"] += 1
                now = time.monotonic()
                if pending_network is not None and pending_network.done():
                    network = pending_network.result()
                    last_probe = now
                    pending_network = None
                if pending_network is None and now - last_probe >= cfg["ping_interval_seconds"]:
                    pending_network = worker.submit(probe_network, cfg)
                if pending_atx is not None and pending_atx.done():
                    atx = pending_atx.result()
                    atx.update(ip=api.ip, checked_unix=time.time())
                    atx_sampled = atx_submitted
                    pending_atx = None
                if api and pending_atx is None and now - atx_submitted >= pikvm.POLL_SECONDS:
                    pending_atx = worker.submit(api.probe)
                    atx_submitted = now
                if now - last_status >= 1:
                    host = tracker.status(now)
                    maintenance = maintenance_on(directory)
                    mode = recovery_mode(directory, api)
                    atx_age = now - atx_sampled
                    atx_view = dict(atx)
                    if math.isfinite(atx_age):
                        atx_view["age_seconds"] = round(atx_age, 2)
                        if atx_age > pikvm.ATX_MAX_AGE and atx["state"] == "reachable":
                            atx_view.update(state="stale", reason="probe_overdue")
                    guard_ready = tracker.guard_ready(now)
                    ups.tick(now, worker, lambda: stop[0],
                             host["state"] == "windows_heartbeat_progressing" and guard_ready)
                    ups_hold = ups.hold_recovery
                    decision = policy.evaluate(now, mode, maintenance or ups_hold, host, atx, atx_age,
                                               tracker.guard_last_ready)
                    if attempt_active and policy.next_reset_at is None:
                        try:
                            budget.finish()
                            attempt_active = False
                        except pikvm.PiKVMError as exc:
                            if not snapshot.get("recovery", {}).get("reset_budget_error"):
                                event("recovery_state_error", {"reason": str(exc)})
                            decision = "reset_budget_error" if decision == "ready_to_reset" else decision
                    if decision == "ready_to_reset" and not select.select([udp], [], [], 0)[0]:
                        committed_at = None

                        def before_send():
                            nonlocal committed_at, attempt_active
                            if (stop[0] or maintenance_on(directory) or recovery_mode(directory, api) != "armed"
                                    or time.monotonic() - atx_sampled > pikvm.ATX_MAX_AGE
                                    or select.select([udp], [], [], 0)[0]):
                                raise pikvm.PiKVMError("reset_conditions_changed")
                            budget.reserve(time.time())
                            attempt_active = True
                            committed_at = time.monotonic()

                        try:
                            if not pikvm.atx_ready(api.probe()):
                                raise pikvm.PiKVMError("reset_atx_preflight_failed")
                            api.reset(before_send)
                            result = "reset_acknowledged"
                        except pikvm.PiKVMError as exc:
                            result = str(exc)
                        if committed_at is not None:
                            policy.note_reset(committed_at, result)
                            decision = "reset_interval_wait"
                        else:
                            policy.defer_reset(time.monotonic(), result)
                            decision = "reset_preflight_wait"
                        event("pikvm_reset_result", {"state": result, "committed": committed_at is not None})
                    now = time.monotonic()
                    show = tracker.show_status(now)
                    outage_remaining = None
                    if decision == "confirming_outage":
                        outage_remaining = max(0, math.ceil(pikvm.OUTAGE_SECONDS - (now - policy.last_contact)))
                    snapshot = {"version": VERSION, "mode": "observe", "process_state": "running",
                                "updated_unix": time.time(), "windows_ip": cfg["windows_ip"],
                                "maintenance": maintenance, "network": network, "pikvm": atx_view,
                                "ups": ups.status(now),
                                "host_heartbeat": host, "accepted": tracker.accepted,
                                "rejected": dict(tracker.rejected), "application_health": show["state"], "show": show,
                                "automatic_restart": mode,
                                "recovery": {"state": decision, "restart_guard_ready": guard_ready,
                                             "ups_hold": ups_hold,
                                             "outage_seconds_remaining": outage_remaining,
                                             "reset_in_seconds": (max(0, math.ceil(policy.next_reset_at - now))
                                                                  if policy.next_reset_at is not None else None),
                                             "last_reset_result": policy.last_reset_result,
                                             "retry_reason": policy.preflight_reason if decision == "reset_preflight_wait" else None,
                                             "reset_budget_error": attempt_active and policy.next_reset_at is None,
                                             "boot_grace_seconds": max(0, math.ceil((policy.boot_grace_until or now) - now))},
                                "schedule": "none"}
                    atomic_json(directory / "status.json", snapshot)
                    current_show = {"Observer": (show["state"], "", "", "")}
                    ups_status = snapshot["ups"]
                    if ups_status["state"] != "unconfigured":
                        current_show["UPS"] = (
                            ups_status["state"], ups_status.get("action", ""),
                            "", ups_status.get("action_reason", ups_status.get("reason", "")),
                        )
                    if show["state"] == "reporting":
                        current_show["MadMapper"] = (show["madmapper"], "", "", "")
                        for name in ("TD_A", "TD_B"):
                            role = show["roles"].get(name, {})
                            current_show[name] = (role.get("state", "unknown"), role.get("fault", ""),
                                                  role.get("process_match", ""), role.get("detail", ""))
                    for name, status in current_show.items():
                        if status != previous_show.get(name):
                            state, fault, match, detail = status
                            event("show_status_changed", {"name": name, "state": state, "fault": fault,
                                                          "process_match": match, "detail": detail}, show_logger)
                    previous_show = current_show
                    signature = (network["state"], host["state"], maintenance, mode, decision,
                                 atx_view["state"], atx_view.get("reason"), atx_view.get("enabled"),
                                 atx_view.get("busy"), atx_view.get("power_led"))
                    if signature != previous:
                        event("state_changed", snapshot)
                        if not quiet:
                            print(f"network={network['state']} | host={host['state']} | PiKVM={atx_view['state']}"
                                  f" | maintenance={maintenance} | recovery={decision}", flush=True)
                        previous = signature
                    if now - last_summary >= cfg["summary_interval_seconds"]:
                        event("summary", snapshot)
                        if not quiet:
                            print("summary: accepted=" + str(tracker.accepted) + " rejected=" + str(sum(tracker.rejected.values())), flush=True)
                        last_summary = now
                    last_status = now
        finally:
            worker.shutdown(wait=False, cancel_futures=True)
            udp.close()
            if snapshot:
                snapshot.update(process_state="stopped", updated_unix=time.time())
                atomic_json(directory / "status.json", snapshot)
            event("monitor_stopped", {"mode": "observe"})
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            for output in (logger, show_logger):
                for handler in list(output.handlers):
                    handler.close()
                    output.removeHandler(handler)


def main():
    parser = argparse.ArgumentParser(description="SF MoMA Oculus host monitor and PiKVM watchdog. Python 3.9+.")
    parser.add_argument("--config", default=str(Path(__file__).resolve().with_name("monitor.config.json")))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--quiet", action="store_true", help="Log to rotating files without console summaries")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--maintenance", choices=("on", "off"))
    actions.add_argument("--configure-pikvm", action="store_true")
    actions.add_argument("--configure-ups", metavar="IPV4")
    actions.add_argument("--recovery", choices=("dry-run", "arm"))
    args = parser.parse_args()
    if args.status:
        path = args.state_dir / "status.json"
        if path.exists():
            data = json_object(path.read_text(encoding="utf-8"))
            age = time.time() - data["updated_unix"]
            data.update(status_age_seconds=round(age, 1), status_fresh=(0 <= age <= 10))
            print(json.dumps(data, indent=2))
        else:
            print("Monitor has not written a status file yet.")
        return
    if args.maintenance:
        directory = private_directory(args.state_dir)
        atomic_json(directory / "maintenance.json", {"enabled": args.maintenance == "on"})
        print("Maintenance " + args.maintenance + ".")
        return
    if args.recovery == "dry-run":
        directory = private_directory(args.state_dir)
        atomic_json(directory / "pikvm-recovery.json", {"armed": False})
        print("PiKVM recovery is in dry-run mode.")
        return
    if args.configure_ups:
        if not sys.stdin.isatty():
            raise ValueError("UPS configuration requires an interactive terminal.")
        directory = private_directory(args.state_dir)
        probe = UPSProbe(directory)
        settings = prompt_credentials(args.configure_ups)
        result = probe.read(settings)
        if result["state"] == "unknown":
            raise ValueError("UPS query failed: " + result["reason"])
        atomic_json(probe.path, settings, durable=True)
        print(json.dumps(result, indent=2))
        return
    if args.configure_pikvm or args.recovery == "arm":
        cfg = read_config(args.config)
        if "pikvm_ip" not in cfg:
            raise ValueError("Configure pikvm_ip in monitor.config.json first")
        directory = private_directory(args.state_dir)
        api = pikvm.PiKVM(cfg["pikvm_ip"], cfg["mac_bind_ip"], directory / "pikvm.credentials.json")
        if args.configure_pikvm:
            if not sys.stdin.isatty():
                raise pikvm.PiKVMError("interactive_terminal_required")
            auth = pikvm.prompt_credentials()
            atx = api.probe(auth)
            if atx["state"] != "reachable":
                raise pikvm.PiKVMError(atx["reason"])
            atomic_json(api.path, auth)
            print(json.dumps(dict(ip=api.ip, **atx), indent=2))
        else:
            status_path = directory / "status.json"
            if not status_path.is_file():
                raise ValueError("Start the monitor and wait for a healthy Windows baseline before arming")
            snapshot = json_object(status_path.read_text(encoding="utf-8"))
            if (snapshot.get("version") != VERSION or snapshot.get("process_state") != "running"
                    or not 0 <= time.time() - snapshot["updated_unix"] <= 10
                    or snapshot["host_heartbeat"]["state"] != "windows_heartbeat_progressing"
                    or not pikvm.atx_ready(api.probe())):
                raise ValueError("Arming requires a fresh monitor snapshot, progressing Windows heartbeat and ready ATX")
            if snapshot.get("recovery", {}).get("restart_guard_ready") is not True:
                raise ValueError("Arming requires Windows Restart Guard to be reporting")
            atomic_json(directory / "pikvm-recovery.json", {"armed": True})
            print("PiKVM recovery is armed. The Mac maintenance flag still inhibits reset.")
        return
    run_monitor(read_config(args.config), args.state_dir, quiet=args.quiet)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, pikvm.PiKVMError) as exc:
        print("Host monitor could not run: " + str(exc), file=sys.stderr)
        if "--quiet" in sys.argv and sys.platform == "darwin":
            try:
                subprocess.run(["/usr/bin/logger", "-t", "org.sfmoma.oculus.hostmonitor",
                                "Host monitor could not run: " + str(exc)[:240]], timeout=2, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        sys.exit(1)
