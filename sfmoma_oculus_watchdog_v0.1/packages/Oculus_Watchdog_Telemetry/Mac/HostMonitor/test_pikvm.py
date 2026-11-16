import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
import json
import math
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest

import mac_host_monitor as monitor
import pikvm
from test_monitor import packet, wire

READY = dict(state="reachable", enabled=True, busy=False, power_led=True)
RUNNING = dict(state="windows_heartbeat_progressing", age_seconds=0)
STALE = dict(state="heartbeat_stale")


class RecoveryTests(unittest.TestCase):
    def evaluate(self, policy, now, host, atx=READY, age=0, mode="dry_run", maintenance=False,
                 guard_last_ready=None):
        if guard_last_ready is None:
            guard_last_ready = now if host["state"] == "windows_heartbeat_progressing" else -math.inf
        return policy.evaluate(now, mode, maintenance, host, atx, age, guard_last_ready)

    def test_guarded_restart_waits_for_new_boot_even_past_grace(self):
        policy = pikvm.RecoveryPolicy("dry_run")
        old_boot = dict(RUNNING, boot_id="old")
        new_boot = dict(RUNNING, boot_id="new")
        self.assertEqual(self.evaluate(policy, 0, old_boot, guard_last_ready=-math.inf),
                         "waiting_for_restart_guard")
        self.evaluate(policy, 1, old_boot)
        policy.note_restart(2)
        self.assertEqual(self.evaluate(policy, 20, old_boot, guard_last_ready=1),
                         "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 22, STALE), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 301, STALE), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 302, STALE), "windows_boot_delayed")
        self.assertEqual(self.evaluate(policy, 3600, STALE), "windows_boot_delayed")
        self.assertEqual(self.evaluate(policy, 3601, new_boot), "observing")
        self.assertIsNone(policy.boot_grace_until)
        self.assertEqual(self.evaluate(policy, 3602, STALE), "confirming_outage")

    def test_power_on_starts_own_grace(self):
        policy = pikvm.RecoveryPolicy("dry_run")
        self.evaluate(policy, 1, dict(RUNNING, boot_id="old"))
        self.evaluate(policy, 20, STALE, atx=dict(READY, power_led=False))
        self.assertEqual(self.evaluate(policy, 100, STALE), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 399, STALE), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 400, STALE), "windows_boot_delayed")
        self.assertEqual(self.evaluate(policy, 410, dict(RUNNING, boot_id="old", stream_id="new")),
                         "observing")

    def test_cancelled_restart_resumes_after_renewed_stable_contact(self):
        policy = pikvm.RecoveryPolicy("dry_run")
        same_session = dict(RUNNING, boot_id="old", stream_id="same")
        self.evaluate(policy, 1, same_session)
        policy.note_restart(2)
        self.assertEqual(self.evaluate(policy, 3, same_session, guard_last_ready=1),
                         "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 15, same_session), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 30, STALE), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 31, same_session), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 50, same_session), "windows_boot_grace")
        self.assertEqual(self.evaluate(policy, 51, same_session), "observing")

    def test_fresh_guard_counts_as_contact(self):
        policy = pikvm.RecoveryPolicy("dry_run")
        self.evaluate(policy, 0, RUNNING)
        self.evaluate(policy, 15, STALE)
        self.assertEqual(self.evaluate(policy, 76, STALE, guard_last_ready=70),
                         "windows_guard_reporting")
        self.assertEqual(self.evaluate(policy, 130, STALE, guard_last_ready=70), "would_reset")

    def test_signed_lifecycle_has_independent_sequence_tracking(self):
        cfg = monitor.read_config(Path(__file__).with_name("monitor.config.json"))
        tracker = monitor.HostTracker(cfg)
        key = bytes(range(32))
        lifecycle = dict(schema="oculus.windows.lifecycle", version=1, host=cfg["windows_host"],
                         stream_id="a"*32, sequence=1, sent_unix=1700000000, phase="ready")
        def accept(packet, wall, now):
            return tracker.accept_lifecycle(monitor.decode_heartbeat(
                wire(packet, key), cfg["windows_ip"], key, cfg, wall), now)
        self.assertFalse(accept(lifecycle, 1700000000, 0))
        self.assertTrue(tracker.guard_ready(1))
        self.assertEqual(tracker.status(1)["state"], "awaiting_windows_heartbeat")
        stopping = dict(lifecycle, sequence=2, phase="stopping")
        self.assertTrue(accept(stopping, 1700000001, 1))
        self.assertFalse(tracker.guard_ready(1))
        with self.assertRaisesRegex(ValueError, "LIFECYCLE_SEQUENCE"):
            accept(stopping, 1700000001, 2)
        new = dict(lifecycle, stream_id="b"*32, sent_unix=1700000002)
        self.assertFalse(accept(new, 1700000002, 2))
        with self.assertRaisesRegex(ValueError, "LIFECYCLE_OLD_STREAM"):
            accept(stopping, 1700000002, 2)

    def test_60_seconds_from_last_signed_contact_with_ready_atx(self):
        policy = pikvm.RecoveryPolicy("dry_run")
        self.assertEqual(self.evaluate(policy, 100, STALE), "waiting_for_healthy_baseline")
        self.assertEqual(self.evaluate(policy, 105, RUNNING), "observing")
        self.assertEqual(self.evaluate(policy, 125, STALE), "confirming_outage")
        self.assertEqual(self.evaluate(policy, 164.99, STALE), "confirming_outage")
        self.assertEqual(self.evaluate(policy, 165, STALE), "would_reset")
        for atx, age in [(READY, 11), (dict(READY, power_led=False), 0), (dict(READY, busy=True), 0)]:
            self.assertEqual(self.evaluate(policy, 166, STALE, atx, age), "atx_unavailable_or_server_off")

    def test_maintenance_mode_changes_and_sender_conflicts_clear_baseline(self):
        for inhibition in ("maintenance", "windows_stopping", "windows_maintenance", "conflicting_host_senders"):
            with self.subTest(inhibition=inhibition):
                policy = pikvm.RecoveryPolicy("dry_run")
                self.evaluate(policy, 0, RUNNING, mode="armed")
                self.evaluate(policy, 16, STALE, mode="armed")
                self.evaluate(policy, 30, {"state": inhibition},
                              mode="armed", maintenance=inhibition == "maintenance")
                self.assertEqual(self.evaluate(policy, 100, STALE, mode="armed"),
                                 "waiting_for_healthy_baseline")
        policy = pikvm.RecoveryPolicy("dry_run")
        self.evaluate(policy, 0, RUNNING)
        self.evaluate(policy, 16, STALE)
        self.assertEqual(self.evaluate(policy, 100, STALE, mode="armed"),
                         "waiting_for_healthy_baseline")

    def test_reset_repeats_every_300_seconds_until_windows_recovers(self):
        policy = pikvm.RecoveryPolicy("armed")
        self.evaluate(policy, 0, RUNNING, mode="armed")
        self.assertEqual(self.evaluate(policy, 59.99, STALE, mode="armed"), "confirming_outage")
        self.assertEqual(self.evaluate(policy, 60, STALE, mode="armed"), "ready_to_reset")
        policy.note_reset(60, "timeout")
        self.assertEqual(self.evaluate(policy, 80, STALE, dict(READY, power_led=False), mode="armed"),
                         "atx_unavailable_or_server_off")
        self.assertEqual(self.evaluate(policy, 359.99, STALE, mode="armed"), "reset_interval_wait")
        self.assertEqual(self.evaluate(policy, 360, STALE, mode="armed"), "ready_to_reset")
        policy.note_reset(360, "reset_acknowledged")
        self.assertEqual(self.evaluate(policy, 659.99, STALE, mode="armed"), "reset_interval_wait")
        self.assertEqual(self.evaluate(policy, 660, STALE, mode="armed"), "ready_to_reset")
        self.assertEqual(self.evaluate(policy, 661, RUNNING, mode="armed"), "observing")
        self.assertIsNone(policy.next_reset_at)
        self.assertEqual(self.evaluate(policy, 721, STALE, mode="armed"), "ready_to_reset")

    def test_boot_progress_delays_retry_until_contact_goes_quiet_again(self):
        policy = pikvm.RecoveryPolicy("armed")
        self.evaluate(policy, 0, RUNNING, mode="armed")
        policy.note_reset(60, "reset_acknowledged")
        self.assertEqual(self.evaluate(policy, 360, {"state": "windows_starting", "age_seconds": 0}, mode="armed"),
                         "waiting_for_windows_heartbeat")
        self.assertEqual(self.evaluate(policy, 370, STALE, mode="armed", guard_last_ready=367),
                         "windows_guard_reporting")
        self.assertEqual(self.evaluate(policy, 426.99, STALE, mode="armed"), "confirming_outage")
        self.assertEqual(self.evaluate(policy, 427, STALE, mode="armed"), "ready_to_reset")

    def test_reset_cadence_survives_reopen_and_corrupt_state_blocks_send(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            budget = monitor.ResetBudget(directory)
            self.assertEqual(monitor.recovery_mode(directory, None), "disabled")
            self.assertEqual(monitor.recovery_mode(directory, object()), "armed")
            monitor.atomic_json(directory / "pikvm-recovery.json", {"armed": False})
            self.assertEqual(monitor.recovery_mode(directory, object()), "dry_run")
            budget.reserve(100)
            reopened = monitor.ResetBudget(directory)
            self.assertEqual(reopened.outstanding_attempt(250, 200), 50)
            recovered = pikvm.RecoveryPolicy("armed", reopened.outstanding_attempt(250, 200), 200)
            self.assertEqual(self.evaluate(recovered, 349.99, STALE, mode="armed"), "reset_interval_wait")
            self.assertEqual(self.evaluate(recovered, 350, STALE, mode="armed"), "ready_to_reset")
            for time_before_due in (99, 399.99):
                with self.assertRaisesRegex(pikvm.PiKVMError, "reset_cooldown"):
                    reopened.reserve(time_before_due)
            reopened.reserve(400)
            reopened.finish()
            self.assertIsNone(reopened.outstanding_attempt(420, 200))
            reopened.reserve(420)
            (directory / "pikvm-reset-budget.json").write_text("{}")
            with self.assertRaisesRegex(pikvm.PiKVMError, "reset_budget_unreadable"):
                reopened.reserve(800)


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
            self.close_connection = True

    def handle_api(self):
        auth = self.headers.get("Authorization")
        self.server.requests.append((self.command, self.path, auth))
        if auth != self.server.expected_auth:
            self.send_error(403)
            return
        result = {"busy": False, "enabled": True, "leds": {"power": True}} if self.command == "GET" else {}
        body = json.dumps({"ok": True, "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = handle_api


class PiKVMTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        cert, key = directory / "cert.pem", directory / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-subj", "/CN=loopback", "-days", "1", "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True, timeout=15)
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        self.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        self.addCleanup(self.server.server_close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.server.requests = []
        self.server.expected_auth = "Basic " + base64.b64encode(b"test:sample").decode("ascii")
        worker = Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.server.shutdown)
        self.api = pikvm.PiKVM("127.0.0.1", "127.0.0.1", directory / "pikvm.credentials.json",
                              port=self.server.server_port)
        self.auth = pikvm.credentials("test", "sample", fingerprint)

    def test_tls_pin_checked_before_http_and_credentials(self):
        self.assertEqual(self.api.probe()["reason"], "credentials_missing")
        self.assertEqual(self.api.probe(dict(self.auth, fingerprint="0" * 64))["reason"], "certificate_mismatch")
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.api.probe(dict(self.auth, password="wrong"))["reason"], "web_auth_rejected")
        self.assertEqual(self.api.probe(self.auth), READY)
        self.assertEqual([row[:2] for row in self.server.requests], [("GET", "/api/atx")] * 2)

    def test_reset_request_requires_pre_send_gate(self):
        monitor.atomic_json(self.api.path, self.auth)

        def gate_closed():
            raise pikvm.PiKVMError("maintenance")

        with self.assertRaisesRegex(pikvm.PiKVMError, "maintenance"):
            self.api.reset(gate_closed)
        self.assertEqual(self.server.requests, [])
        self.api.reset(lambda: None)
        self.assertEqual(self.server.requests, [
            ("POST", "/api/atx/power?action=reset_hard&wait=1", self.server.expected_auth)
        ])


    def test_live_monitor_dry_run_armed_retry_and_recovery(self):
        directory = self.api.path.parent
        monitor.atomic_json(self.api.path, self.auth)
        monitor.atomic_json(directory / "pikvm-recovery.json", {"armed": False})
        key = monitor.get_key(directory)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            udp_port = port_probe.getsockname()[1]

        fixture = """
from functools import partial
from pathlib import Path
import sys
import time
import mac_host_monitor as monitor
import pikvm

state = Path(sys.argv[1])
cfg = monitor.read_config(Path('monitor.config.json'))
cfg.update(mac_bind_ip='127.0.0.1', windows_ip='127.0.0.1', pikvm_ip='127.0.0.1',
           heartbeat_port=int(sys.argv[2]), heartbeat_timeout_seconds=3,
           stability_seconds=0, ping_interval_seconds=1)
def probe_network(cfg):
    if (state / "icmp-block").exists():
        (state / "icmp-started").touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (state / "icmp-release").exists():
            time.sleep(0.02)
    return {"state": "no_icmp_reply" if (state / "icmp-loss").exists() else "icmp_reply"}

monitor.probe_network = probe_network


class FaultyPiKVM(pikvm.PiKVM):
    def reset(self, before_send):
        if not (state / "transport-restored").exists():
            raise pikvm.PiKVMError("connection_or_tls_error")
        return super().reset(before_send)
pikvm.PiKVM = partial(FaultyPiKVM, port=int(sys.argv[3]))
pikvm.POLL_SECONDS = 0.25
pikvm.OUTAGE_SECONDS = 1
pikvm.RESTART_GUARD_TIMEOUT = 3
pikvm.RESET_INTERVAL_SECONDS = 4
monitor.run_monitor(cfg, state)
"""
        command = [sys.executable, "-c", fixture, str(directory), str(udp_port), str(self.api.port)]
        with (directory / "console.log").open("w+") as console, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            process = subprocess.Popen(command, cwd=Path(__file__).resolve().parent,
                                       stdout=console, stderr=subprocess.STDOUT)
            sequence = 0
            boot = 1

            def send_notice(phase):
                notice = dict(schema="oculus.windows.lifecycle", version=1, host="CP007-01",
                              stream_id=str(boot)*32, sequence=sequence, sent_unix=int(time.time()), phase=phase)
                udp.sendto(wire(notice, key), ("127.0.0.1", udp_port))

            def send_heartbeat():
                nonlocal sequence
                sequence += 1
                msg = wire(dict(packet(sequence, stamp=int(time.time())), boot_id=f"boot-{boot}"), key)
                udp.sendto(msg, ("127.0.0.1", udp_port))
                send_notice("ready")

            def wait_for(predicate, keep_sending=False):
                deadline = time.monotonic() + 9
                while time.monotonic() < deadline and process.poll() is None:
                    if keep_sending:
                        send_heartbeat()
                    try:
                        status = json.loads((directory / "status.json").read_text())
                    except FileNotFoundError:
                        status = {}
                    if predicate(status):
                        return status
                    time.sleep(0.1)
                console.seek(0)
                self.fail("Monitor did not reach the expected state. Console:\n"
                          + console.read() + "\nLast status: " + str(locals().get("status")))

            def ready(status, mode):
                return (status.get("automatic_restart") == mode
                        and status.get("host_heartbeat", {}).get("state") == "windows_heartbeat_progressing"
                        and status.get("pikvm", {}).get("power_led") is True
                        and status.get("network", {}).get("state") == "icmp_reply"
                        and status.get("recovery", {}).get("restart_guard_ready") is True)

            def requests_to_reset():
                return [row for row in self.server.requests if row[0] == "POST"]

            try:
                wait_for(lambda status: ready(status, "dry_run"), keep_sending=True)
                sequence += 1
                send_notice("stopping")
                (directory / "icmp-loss").touch()
                wait_for(lambda status: status.get("recovery", {}).get("state") == "windows_boot_grace"
                         and status.get("host_heartbeat", {}).get("state") == "heartbeat_stale")
                self.assertEqual(requests_to_reset(), [])
                boot = 2
                (directory / "icmp-loss").unlink()
                wait_for(lambda status: ready(status, "dry_run")
                         and status["recovery"]["state"] == "observing", keep_sending=True)
                (directory / "icmp-loss").touch()
                wait_for(lambda status: status.get("recovery", {}).get("state") == "would_reset")
                self.assertEqual(requests_to_reset(), [])
                self.assertFalse((directory / "pikvm-reset-budget.json").exists())

                (directory / "icmp-loss").unlink()
                monitor.atomic_json(directory / "pikvm-recovery.json", {"armed": True})
                wait_for(lambda status: ready(status, "armed"), keep_sending=True)
                wait_for(lambda status: status.get("recovery", {}).get("state") == "reset_preflight_wait")
                self.assertFalse(requests_to_reset())
                self.assertFalse((directory / "pikvm-reset-budget.json").exists())
                (directory / "transport-restored").touch()
                wait_for(lambda status: status.get("recovery", {}).get("state") == "reset_interval_wait")
                reset_budget = json.loads((directory / "pikvm-reset-budget.json").read_text())
                self.assertTrue(reset_budget["active"])
                time.sleep(1.2)
                self.assertEqual(len(requests_to_reset()), 1)
                wait_for(lambda status: len(requests_to_reset()) == 2)
                self.assertGreaterEqual(json.loads((directory / "pikvm-reset-budget.json").read_text())["attempted_unix"],
                                        reset_budget["attempted_unix"] + 4)
                wait_for(lambda status: ready(status, "armed")
                         and status["recovery"]["state"] == "observing", keep_sending=True)
                self.assertFalse(json.loads((directory / "pikvm-reset-budget.json").read_text())["active"])
                self.assertEqual(len(requests_to_reset()), 2)

                (directory / "icmp-block").touch()
                started = wait_for(lambda status: (directory / "icmp-started").exists(), keep_sending=True)
                wait_for(lambda status: status.get("accepted", 0) > started["accepted"]
                         and status.get("pikvm", {}).get("checked_unix", 0) > started["pikvm"]["checked_unix"],
                         keep_sending=True)
            finally:
                (directory / "icmp-release").touch()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
