import json
import math
import time

from pikvm import PiKVMError
from ups_snmp import UPSProbe
from windows_power import PowerError


POLL_SECONDS = 10
MAX_SAMPLE_GAP = 25
CONFIRM_MAX_AGE = 5
BATTERY_COUNTER_TOLERANCE_SECONDS = 2
BATTERY_SECONDS = 600
RESTORE_SECONDS = 180
PREFLIGHT_RETRY_SECONDS = 30
AC_STATES = {"utility", "bypass", "boost", "trim"}
RECORD_STATES = {"shutdown_reserved", "shutdown_sent", "shutdown_unconfirmed",
                 "power_on_reserved", "power_on_sent", "power_on_unconfirmed"}


class UPSAutomation:
    def __init__(self, directory, windows, pikvm, save, maintenance):
        self.probe = UPSProbe(directory)
        self.windows = windows
        self.pikvm = pikvm
        self.save = save
        self.maintenance = maintenance
        self.record_path = directory / "ups-power-cycle.json"
        self.record = self.read_record()
        self.reading = {"state": "unconfigured"}
        self.sampled = -math.inf
        self.power_since = None
        self.pending = None
        self.last_poll = -math.inf
        self.retry_at = 0
        self.problem = None

    def read_record(self):
        try:
            state = json.loads(self.record_path.read_text(encoding="utf-8"))["state"]
            return state if type(state) is str and state in RECORD_STATES else "record_error"
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return "record_error"

    @property
    def hold_recovery(self):
        return self.record is not None or (self.pending is not None and self.pending[0] != "poll")

    @property
    def writing(self):
        return self.pending is not None and self.pending[0] == "write"

    def record_state(self, state):
        try:
            self.save(self.record_path, {"state": state}, durable=True)
        except OSError:
            self.problem = "record_error"
            return False
        self.record = state
        return True

    def clear_record(self):
        try:
            self.record_path.unlink(missing_ok=True)
        except OSError:
            self.problem = "record_error"
            return
        self.record = None
        self.problem = None

    def sample(self):
        reading = self.probe.read()
        return time.monotonic(), reading

    def accept(self, sampled, reading):
        previous = self.reading["state"]
        current = reading["state"]
        same_source = current == previous or (previous in AC_STATES and current in AC_STATES)
        old_events = self.reading.get("input_events")
        new_events = reading.get("input_events")
        ac_interrupted = (previous in AC_STATES and current in AC_STATES
                          and old_events is not None and new_events is not None and old_events != new_events)
        if not same_source or ac_interrupted or sampled - self.sampled > MAX_SAMPLE_GAP:
            self.power_since = sampled if current == "battery" or current in AC_STATES else None
            self.problem = None
            self.retry_at = 0
        battery_seconds = reading.get("battery_seconds")
        if current == "battery" and self.power_since is not None and battery_seconds is not None:
            counter_start = sampled - battery_seconds
            if counter_start - self.power_since > BATTERY_COUNTER_TOLERANCE_SECONDS:
                self.power_since = counter_start
                self.problem = None
                self.retry_at = 0
        self.reading, self.sampled = reading, sampled

    def due(self, now):
        if self.power_since is None or now - self.sampled > MAX_SAMPLE_GAP:
            return None
        state = self.reading["state"]
        if self.record is None and state == "battery" and now - self.power_since >= BATTERY_SECONDS:
            return "shutdown"
        if self.record == "shutdown_sent" and state in AC_STATES and now - self.power_since >= RESTORE_SECONDS:
            return "power_on"
        return None

    def prepare(self, action):
        problem = None
        if action == "shutdown":
            try:
                self.windows.send("status")
            except PowerError:
                problem = "windows_ssh_unavailable"
        elif self.pikvm is None:
            problem = "pikvm_unavailable"
        else:
            atx = self.pikvm.probe()
            if (atx.get("state") != "reachable" or atx.get("enabled") is not True
                    or atx.get("busy") is not False or type(atx.get("power_led")) is not bool):
                problem = "pikvm_unavailable"
            elif atx["power_led"]:
                problem = "already_powered"
        sampled, reading = self.sample()
        return sampled, reading, problem

    def issue(self, action, sampled, stopping):
        dispatched = False

        def before_send():
            nonlocal dispatched
            if stopping() or self.maintenance() or time.monotonic() - sampled > CONFIRM_MAX_AGE:
                raise PiKVMError("ups_conditions_changed")
            dispatched = True

        try:
            if action == "shutdown":
                before_send()
                self.windows.send("shutdown")
            else:
                self.pikvm.request("POST", "/api/atx/power?action=on&wait=0",
                                   self.pikvm.read_credentials(), before_send)
        except (PowerError, PiKVMError):
            return action + "_unconfirmed" if dispatched else "cancelled_before_send"
        return action + "_sent"

    def finish_write(self, result, now):
        if result != "cancelled_before_send":
            self.record_state(result)
            return
        if self.record == "power_on_reserved":
            restored = self.record_state("shutdown_sent")
        else:
            self.clear_record()
            restored = self.record is None
        if restored:
            self.problem = "cancelled_before_send"
        self.retry_at = now + PREFLIGHT_RETRY_SECONDS

    def tick(self, now, executor, stopping, windows_healthy):
        if self.pending:
            kind, future = self.pending
            if not future.done():
                return
            self.pending = None
            if kind == "write":
                self.finish_write(future.result(), now)
            elif kind == "poll":
                self.accept(*future.result())
            else:
                sampled, reading, problem = future.result()
                self.accept(sampled, reading)
                if not stopping() and self.due(now) == kind and not self.maintenance():
                    if problem == "already_powered" and windows_healthy:
                        self.clear_record()
                    elif problem:
                        self.problem = problem
                        self.retry_at = now + PREFLIGHT_RETRY_SECONDS
                    elif self.record_state(kind + "_reserved"):
                        self.pending = ("write", executor.submit(self.issue, kind, sampled, stopping))
                        return
                    else:
                        self.retry_at = now + PREFLIGHT_RETRY_SECONDS

        if now - self.sampled > MAX_SAMPLE_GAP and self.reading["state"] != "unconfigured":
            self.reading = {"state": "unknown", "reason": "ups_sample_stale"}
            self.power_since = None
        if (self.record in RECORD_STATES and self.record != "shutdown_sent"
                and windows_healthy and self.reading["state"] in AC_STATES
                and self.power_since is not None and now - self.power_since >= RESTORE_SECONDS):
            self.clear_record()
        action = self.due(now)
        if action and not stopping() and not self.maintenance() and now >= self.retry_at:
            self.pending = (action, executor.submit(self.prepare, action))
        elif not stopping() and now - self.last_poll >= POLL_SECONDS:
            self.last_poll = now
            self.pending = ("poll", executor.submit(self.sample))

    def status(self, now):
        view = {name: self.reading[name] for name in ("state", "source_code", "reason") if name in self.reading}
        if now - self.sampled > MAX_SAMPLE_GAP and view["state"] != "unconfigured":
            view = {"state": "unknown", "reason": "ups_sample_stale"}
        if self.writing:
            view["write_in_progress"] = True
            view["action"] = self.record
        elif self.pending and self.pending[0] in {"shutdown", "power_on"}:
            view["action"] = self.pending[0] + "_checking"
        elif self.power_since is not None and (
                self.record is None and view["state"] == "battery"
                or self.record == "shutdown_sent" and view["state"] in AC_STATES):
            restoring = self.record == "shutdown_sent"
            interval = RESTORE_SECONDS if restoring else BATTERY_SECONDS
            remaining = max(0, math.ceil(interval - (now - self.power_since)))
            view.update(action="power_on_wait" if restoring else "shutdown_wait",
                        seconds_remaining=remaining)
            if remaining == 0 and self.maintenance():
                view["action"] = "maintenance_hold"
        elif self.record:
            view["action"] = self.record
        if self.problem:
            view["action_reason"] = self.problem
        return view
