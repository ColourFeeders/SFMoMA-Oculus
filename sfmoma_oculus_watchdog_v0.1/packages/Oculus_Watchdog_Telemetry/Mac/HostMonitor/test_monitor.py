#!/usr/bin/env python3
import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import mac_host_monitor as monitor
import background_service
import plistlib

ROOT = Path(__file__).resolve().parent
KEY = bytes(range(32))


def config():
    return monitor.read_config(ROOT / "monitor.config.json")


def packet(sequence=1, stream="1" * 32, phase="running", stamp=1700000000):
    return dict(schema="oculus.windows.host", version=1, host="CP007-01",
                boot_id="test-boot", stream_id=stream, sequence=sequence,
                sent_unix=stamp, phase=phase)


def wire(obj, key=KEY):
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return json.dumps(dict(payload_b64=base64.b64encode(payload).decode("ascii"),
                           hmac_sha256=hmac.new(key, payload, hashlib.sha256).hexdigest())).encode("utf-8")


class ProtocolTests(unittest.TestCase):
    def test_show_data_expires_independently_of_host_heartbeat(self):
        cfg = config()
        tracker = monitor.HostTracker(cfg)
        wall = 1700000000
        sample = {"state": "reporting", "sampled_ms": wall * 1000, "madmapper": "running",
                  "roles": {"TD_A": {"state": "Healthy", "process_match": "matched", "age_seconds": 0.2}},
                  "accepted_packets": 100, "rejected_packets": 0}
        tracker.accept(monitor.decode_heartbeat(wire(packet()), cfg["windows_ip"], KEY, cfg, wall), 0)
        self.assertEqual(tracker.show_status(0)["state"], "waiting")
        signed = dict(packet(2), show=sample)
        tracker.accept(monitor.decode_heartbeat(wire(signed), cfg["windows_ip"], KEY, cfg, wall), 1)
        status = tracker.show_status(2)
        self.assertEqual(status["state"], "reporting")
        self.assertEqual(status["roles"]["TD_A"]["age_seconds"], 1.2)
        self.assertEqual(tracker.show_status(7), {"state": "stale"})
        bad_show = dict(packet(3), show={"state": "reporting", "sampled_ms": "invalid"})
        tracker.accept(monitor.decode_heartbeat(wire(bad_show), cfg["windows_ip"], KEY, cfg, wall), 10)
        self.assertEqual(tracker.show_status(10), {"state": "unreadable"})
        self.assertEqual(tracker.status(10)["state"], "windows_heartbeat_progressing")

    def test_valid_and_tampered_authentication(self):
        cfg = config()
        self.assertEqual(monitor.decode_heartbeat(wire(packet()), cfg["windows_ip"], KEY, cfg, 1700000000)["sequence"], 1)
        with self.assertRaises(ValueError):
            monitor.decode_heartbeat(wire(packet(), b"x" * 32), cfg["windows_ip"], KEY, cfg, 1700000000)
        with self.assertRaises(ValueError):
            monitor.decode_heartbeat(wire(packet()), "10.1.80.66", KEY, cfg, 1700000000)

    def test_strict_fields_clock_and_packet_limits(self):
        cfg = config()
        for field, value in (("version", True), ("sequence", True), ("sequence", 0),
                             ("stream_id", ["1" * 32]), ("host", "WRONG_HOST"),
                             ("phase", "unknown"), ("sent_unix", 1600000000)):
            obj = packet(); obj[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                monitor.decode_heartbeat(wire(obj), cfg["windows_ip"], KEY, cfg, 1700000000)
        for raw in (b"not json", b"[{}]", b"x" * (monitor.MAX_PACKET + 1), b"\xff"):
            with self.assertRaises(ValueError):
                monitor.decode_heartbeat(raw, cfg["windows_ip"], KEY, cfg, 1700000000)
        with self.assertRaises(ValueError):
            monitor.json_object('{"a":1,"a":2}')
        nested = b'{"payload_b64":' + b'[' * 1200 + b'0' + b']' * 1200 + b',"hmac_sha256":"' + b'0' * 64 + b'"}'
        self.assertLess(len(nested), monitor.MAX_PACKET)
        with self.assertRaises(ValueError):
            monitor.decode_heartbeat(nested, cfg["windows_ip"], KEY, cfg, 1700000000)

    def test_unknown_is_not_a_crash_and_sequences_do_not_renew_liveness(self):
        tracker = monitor.HostTracker(config())
        self.assertEqual(tracker.status(100000)["state"], "awaiting_windows_heartbeat")
        tracker.accept(packet(), 0)
        self.assertEqual(tracker.status(12)["state"], "validating_host_heartbeat")
        with self.assertRaises(ValueError):
            tracker.accept(packet(), 14)
        self.assertEqual(tracker.status(15)["state"], "heartbeat_stale")
        tracker.accept(packet(2), 16)
        self.assertEqual(tracker.status(16)["state"], "validating_host_heartbeat")
        for stamp in (20, 24, 26):
            tracker.accept(packet(stamp), stamp)
        self.assertEqual(tracker.status(26)["state"], "windows_heartbeat_progressing")
        tracker.accept(packet(27, phase="maintenance"), 27)
        self.assertEqual(tracker.status(27)["state"], "windows_maintenance")

    def test_competing_senders_reset_stability(self):
        tracker = monitor.HostTracker(config())
        tracker.accept(packet(), 0)
        tracker.accept(packet(2), 10)
        self.assertEqual(tracker.status(10)["state"], "windows_heartbeat_progressing")
        tracker.accept(packet(1, stream="2" * 32), 11)
        self.assertEqual(tracker.status(11)["state"], "conflicting_host_senders")
        tracker.accept(packet(2, stream="2" * 32), 25)
        self.assertEqual(tracker.status(25)["state"], "validating_host_heartbeat")

    def test_private_key_persists_and_maintenance_defaults_off(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = monitor.private_directory(Path(temp) / "State With Spaces")
            key = monitor.get_key(directory)
            self.assertEqual(len(key), 32)
            self.assertEqual(key, monitor.get_key(directory))
            self.assertEqual((directory / "host-heartbeat.key").stat().st_mode & 0o777, 0o600)
            self.assertFalse(monitor.maintenance_on(directory))
            monitor.atomic_json(directory / "maintenance.json", dict(enabled=True))
            self.assertTrue(monitor.maintenance_on(directory))
            monitor.atomic_json(directory / "maintenance.json", dict(enabled="false"))
            self.assertTrue(monitor.maintenance_on(directory))

    def test_native_ping_is_bounded_and_source_specific(self):
        cfg = config()
        with patch.object(monitor.sys, "platform", "darwin"), patch.object(monitor.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(monitor.probe_network(cfg)["state"], "icmp_reply")
            self.assertIn(cfg["mac_bind_ip"], run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["timeout"], 3)
            run.return_value.returncode = 2
            self.assertEqual(monitor.probe_network(cfg)["state"], "no_icmp_reply")
            run.side_effect = subprocess.TimeoutExpired("ping", 3)
            self.assertEqual(monitor.probe_network(cfg)["state"], "probe_timeout")

    def test_configuration_cannot_enable_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = config(); cfg["mode"] = "restart"
            path = Path(temp) / "config.json"; path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                monitor.read_config(path)

    def test_background_job_is_non_root_and_handles_paths_with_spaces(self):
        definition = background_service.job_definition(
            'sfmoma', 'staff', Path('/Library/Developer/CommandLineTools/usr/bin/python3'),
            Path('/Library/Application Support/SFMoMAOculus/HostMonitor'),
            Path('/Users/sfmoma/Library/Application Support/Test & Monitor'))
        self.assertEqual(plistlib.loads(plistlib.dumps(definition)), definition)
        self.assertEqual(definition['UserName'], 'sfmoma')
        self.assertIn('--quiet', definition['ProgramArguments'])
        self.assertTrue(definition['KeepAlive'])
        self.assertEqual(definition['StandardOutPath'], '/dev/null')
        with self.assertRaises(ValueError):
            background_service.job_definition('root', 'wheel', Path('/python'), Path('/app'), Path('/state'))

    def test_background_removal_waits_for_launchd_to_finish(self):
        definition = background_service.job_definition(
            "sfmoma", "staff", Path("/python"), background_service.APP, Path("/state"))
        with tempfile.TemporaryDirectory() as temp:
            installed = Path(temp) / "hostmonitor.plist"
            installed.write_bytes(plistlib.dumps(definition))
            for times_out in (False, True):
                with (
                    self.subTest(times_out=times_out),
                    patch.object(background_service, "PLIST", installed),
                    patch.object(background_service, "job_loaded", side_effect=(True, True, False)) as loaded,
                    patch.object(background_service, "admin") as admin,
                    patch.object(background_service.subprocess, "run"),
                    patch.object(background_service.sys, "argv", ["background_service.py", "remove"]),
                    patch.object(background_service.sys, "platform", "darwin"),
                    patch.object(background_service.os, "geteuid", return_value=501),
                    patch.object(background_service.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="sfmoma")),
                    patch.object(background_service.time, "monotonic", side_effect=(0, 15 if times_out else 0)),
                    patch.object(background_service.time, "sleep") as sleep,
                ):
                    bootout = ["/bin/launchctl", "bootout", "system/" + background_service.LABEL]
                    if times_out:
                        with self.assertRaisesRegex(RuntimeError, "still unloading"):
                            background_service.main()
                        admin.assert_called_once_with(bootout)
                    else:
                        def apply_command(arguments):
                            if arguments[0] == "/bin/rm":
                                self.assertEqual(loaded.call_count, 3)
                        admin.side_effect = apply_command
                        background_service.main()
                        self.assertEqual(admin.call_args_list[0].args[0], bootout)
                        admin.assert_called_with(["/bin/rm", "-f", str(installed)])
                        sleep.assert_called_once_with(0.2)

    def test_installer_requires_status_from_the_new_monitor(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            status = state / "status.json"
            monitor.atomic_json(status, dict(version="0.2.2", process_state="running", updated_unix=10))
            with patch.object(background_service.time, "monotonic", side_effect=(0, 0, 20)), \
                 patch.object(background_service.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "no fresh status"):
                    background_service.wait_for_monitor(state, 5)
            monitor.atomic_json(status, dict(version=monitor.VERSION, process_state="running", updated_unix=10))
            background_service.wait_for_monitor(state, 5)

    def test_rotating_event_logs_have_bounded_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            logger = monitor.make_logger(Path(temp))
            logger.handlers[0].maxBytes = 250
            try:
                for number in range(30):
                    logger.info(json.dumps(dict(event='test', number=number, detail='x' * 80)))
                files = list(Path(temp).glob('events.jsonl*'))
                self.assertLessEqual(len(files), 5)
                for path in files:
                    self.assertLessEqual(path.stat().st_size, 250)
            finally:
                for handler in list(logger.handlers):
                    handler.close(); logger.removeHandler(handler)


class LiveReceiverTests(unittest.TestCase):
    def test_real_udp_status_logging_single_instance_and_stale_detection(self):
        with tempfile.TemporaryDirectory(prefix="oculus-mac-test-") as temp:
            directory = Path(temp)
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
            cfg = config()
            cfg.update(mac_bind_ip="127.0.0.1", windows_ip="127.0.0.1", heartbeat_port=port,
                       heartbeat_timeout_seconds=3, stability_seconds=1, ping_interval_seconds=300)
            cfgpath = directory / "Test Config.json"; cfgpath.write_text(json.dumps(cfg))
            state = directory / "State With Spaces"
            command = [sys.executable, str(ROOT / "mac_host_monitor.py"), "--config", str(cfgpath), "--state-dir", str(state)]
            console = (directory / "console.txt").open("w")
            process = subprocess.Popen(command, stdout=console, stderr=subprocess.STDOUT)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            def status():
                if process.poll() is not None:
                    raise AssertionError((directory / "console.txt").read_text())
                file = state / "status.json"
                return json.loads(file.read_text()) if file.exists() else None
            def wait_for(expected, producer=None, timeout=8):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if producer:
                        producer()
                    data = status()
                    if data and data["host_heartbeat"]["state"] == expected:
                        return data
                    time.sleep(0.1)
                raise AssertionError("State not reached: " + expected + " / " + str(status()))
            try:
                initial = wait_for("awaiting_windows_heartbeat")
                self.assertFalse(initial["maintenance"])
                self.assertEqual(initial["automatic_restart"], "armed")
                self.assertEqual(initial["recovery"]["state"], "waiting_for_healthy_baseline")
                key = bytes.fromhex((state / "host-heartbeat.key").read_text().strip())
                sequence = [0]
                def send_progress():
                    sequence[0] += 1
                    sender.sendto(wire(packet(sequence[0], stamp=int(time.time())), key), ("127.0.0.1", port))
                live = wait_for("windows_heartbeat_progressing", send_progress)
                self.assertGreater(live["accepted"], 1)
                duplicate = wire(packet(sequence[0], stamp=int(time.time())), key)
                stale = wait_for("heartbeat_stale", lambda: sender.sendto(duplicate, ("127.0.0.1", port)))
                self.assertGreater(stale["rejected"].get("SEQUENCE_NOT_ADVANCING", 0), 0)
                second = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("Another host monitor", second.stderr)
                self.assertIn('"event": "state_changed"', (state / "events.jsonl").read_text())
            finally:
                sender.close()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
                console.close()
            self.assertEqual(json.loads((state / "status.json").read_text())["process_state"], "stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
