from concurrent.futures import Future
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mac_host_monitor import atomic_json
from pikvm import PiKVMError
from ups_automation import UPSAutomation
from ups_snmp import UPSProbe, credentials
from windows_power import PowerError


class InlineExecutor:
    def submit(self, callback, *args):
        future = Future()
        future.set_result(callback(*args))
        return future


class UPSTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.windows = Mock()
        self.pikvm = Mock()
        self.pikvm.probe.return_value = {
            "state": "reachable", "enabled": True, "busy": False, "power_led": False,
        }
        self.pikvm.request.side_effect = lambda method, path, auth, gate: gate()
        self.maintenance = False
        self.controller = self.fresh_controller()
        self.executor = InlineExecutor()
        self.clock = patch("ups_automation.time.monotonic")
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def fresh_controller(self):
        return UPSAutomation(self.directory, self.windows, self.pikvm, atomic_json,
                             lambda: self.maintenance)

    def step(self, now, state, healthy=False, **counters):
        self.controller.probe = Mock(read=Mock(return_value=dict(state=state, **counters)))
        for offset in (0, 0.1, 0.2, 0.3, 0.4):
            self.now.return_value = now + offset
            self.controller.tick(now + offset, self.executor, lambda: False, healthy)

    def feed(self, start, end, state):
        for now in range(start, end + 1, 10):
            self.step(now, state)

    def windows_actions(self):
        return [call.args[0] for call in self.windows.send.call_args_list]

    def test_battery_then_restore_times_and_exactly_one_action(self):
        self.feed(0, 590, "battery")
        self.assertEqual(self.windows_actions(), [])
        self.assertEqual(self.controller.status(590)["seconds_remaining"], 10)
        self.step(600, "battery")
        self.assertEqual(self.windows_actions(), ["status", "shutdown"])
        self.assertEqual(json.loads(self.controller.record_path.read_text())["state"], "shutdown_sent")
        self.assertTrue(self.controller.hold_recovery)
        self.feed(610, 690, "battery")
        self.assertEqual(self.windows_actions(), ["status", "shutdown"])
        self.feed(700, 870, "utility")
        self.pikvm.request.assert_not_called()
        self.assertEqual(self.controller.status(870.4)["seconds_remaining"], 10)
        self.step(880, "utility")
        self.pikvm.request.assert_called_once()
        self.assertEqual(self.pikvm.request.call_args.args[:2],
                         ("POST", "/api/atx/power?action=on&wait=0"))
        self.assertTrue(self.controller.hold_recovery)
        self.step(890, "utility", healthy=True)
        self.assertFalse(self.controller.hold_recovery)
        self.assertFalse(self.controller.record_path.exists())
        self.pikvm.request.assert_called_once()

    def test_utility_recheck_at_deadline_cancels_shutdown(self):
        self.feed(0, 590, "battery")
        self.step(600, "utility")
        self.assertEqual(self.windows_actions(), ["status"])
        self.assertIsNone(self.controller.record)
        self.assertEqual(self.controller.status(600.4), {"state": "utility"})

    def test_battery_counter_restarts_timer_after_transfer_between_polls(self):
        for now in range(0, 600, 10):
            self.step(now, "battery", battery_seconds=now + 5)
        self.step(600, "battery", battery_seconds=1)
        self.assertEqual(self.windows_actions(), ["status"])
        self.assertEqual(self.controller.status(600.4)["seconds_remaining"], 599)

    def test_ac_timer_restarts_after_input_interruption_between_polls(self):
        self.assertTrue(self.controller.record_state("shutdown_sent"))
        for now in range(0, 180, 10):
            self.step(now, "utility", input_events=2)
        self.step(180, "utility", input_events=3)
        self.pikvm.request.assert_not_called()
        self.assertEqual(self.controller.status(180.4)["seconds_remaining"], 180)
        self.feed(190, 360, "utility")
        self.pikvm.request.assert_called_once()

    def test_battery_return_and_unknown_restarts_ac_timer_across_reload(self):
        self.assertTrue(self.controller.record_state("shutdown_sent"))
        self.controller = self.fresh_controller()
        self.feed(0, 170, "utility")
        self.step(180, "battery")
        self.feed(190, 310, "utility")
        self.step(320, "unknown")
        self.feed(330, 500, "utility")
        self.pikvm.request.assert_not_called()
        self.step(510, "utility")
        self.pikvm.request.assert_called_once()
        self.windows.send.assert_not_called()

    def test_maintenance_and_unconfirmed_shutdown_inhibit_automatic_boot(self):
        self.maintenance = True
        self.feed(0, 600, "battery")
        self.windows.send.assert_not_called()
        self.assertEqual(self.controller.status(600)["action"], "maintenance_hold")
        self.maintenance = False
        self.windows.send.side_effect = lambda action: self.fail_shutdown(action)
        self.step(610, "battery")
        self.assertEqual(self.controller.record, "shutdown_unconfirmed")
        self.controller = self.fresh_controller()
        self.feed(620, 820, "utility")
        self.pikvm.request.assert_not_called()

    def test_restore_leaves_an_already_running_server_alone(self):
        self.assertTrue(self.controller.record_state("shutdown_sent"))
        self.pikvm.probe.return_value["power_led"] = True
        self.feed(0, 170, "utility")
        self.step(180, "utility", healthy=True)
        self.pikvm.request.assert_not_called()
        self.assertFalse(self.controller.hold_recovery)

    def test_power_on_retries_a_failure_before_dispatch(self):
        self.assertTrue(self.controller.record_state("shutdown_sent"))
        self.pikvm.request.side_effect = PiKVMError("certificate_mismatch")
        self.feed(0, 180, "utility")
        self.assertEqual(self.controller.record, "shutdown_sent")
        self.assertEqual(self.controller.status(180.4)["action_reason"], "cancelled_before_send")
        self.pikvm.request.side_effect = lambda method, path, auth, gate: gate()
        self.feed(190, 220, "utility")
        self.assertEqual(self.pikvm.request.call_count, 2)
        self.assertEqual(self.controller.record, "power_on_sent")

    def test_power_on_awaits_operator_after_unconfirmed_dispatch(self):
        def drop_response(method, path, auth, gate):
            gate()
            raise PiKVMError("timeout")

        self.assertTrue(self.controller.record_state("shutdown_sent"))
        self.pikvm.request.side_effect = drop_response
        self.feed(0, 260, "utility")
        self.pikvm.request.assert_called_once()
        self.assertEqual(self.controller.record, "power_on_unconfirmed")
        self.assertTrue(self.controller.hold_recovery)

    def test_maintenance_before_send_does_not_consume_shutdown_attempt(self):
        self.assertTrue(self.controller.record_state("shutdown_reserved"))
        self.maintenance = True
        self.now.return_value = 600
        result = self.controller.issue("shutdown", 600, lambda: False)
        self.controller.finish_write(result, 600)
        self.windows.send.assert_not_called()
        self.assertIsNone(self.controller.record)
        self.assertFalse(self.controller.record_path.exists())

    @staticmethod
    def fail_shutdown(action):
        if action == "shutdown":
            raise PowerError("Missing acknowledgement")

    def test_power_probe_keeps_passphrases_out_of_command_arguments(self):
        probe = UPSProbe(self.directory)
        self.assertEqual(probe.read(), {"state": "unconfigured"})
        settings = credentials("192.0.2.14", "readonly", "TEST_AUTH", "TEST_PRIV")
        atomic_json(probe.path, settings)
        reply = '{"source_code":5, "battery_seconds":300, "input_events":"unavailable"}'
        with patch("ups_snmp.subprocess.run", return_value=SimpleNamespace(stdout=reply)) as run:
            self.assertEqual(probe.read(), {"state": "battery", "source_code": 5, "battery_seconds": 300})
        command = run.call_args.args[0]
        self.assertNotIn("TEST_AUTH", str(command))
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), settings)
        probe.path.chmod(0o644)
        self.assertEqual(probe.read(), {"state": "unknown", "reason": "ups_credentials_permissions"})


if __name__ == "__main__":
    unittest.main()
