"use strict";

const element = id => document.getElementById(id);
function text(id, value) {
  const target = element(id);
  if (target.textContent !== value) target.textContent = value;
}

const watchdogStates = {
  running: ["Watchdog running", "Receiving fresh status from the Mac watchdog.", "good"],
  stale: ["Watchdog status stale", "The watchdog has not published an update in over 10 seconds.", "bad"],
  stopped: ["Watchdog stopped", "The watchdog reported that it stopped.", "bad"],
  waiting: ["Waiting for watchdog", "The Mac watchdog has not published a status file yet.", "warning"],
  unreadable: ["Status unavailable", "The display could not read the watchdog status file.", "bad"],
  clock_mismatch: ["Check the Mac clock", "The watchdog snapshot is dated in the future.", "warning"],
  disconnected: ["Display disconnected", "The local display service is unreachable. Retrying.", "bad"],
};
const windowsStates = {
  awaiting_windows_heartbeat: ["Waiting for heartbeat", "warning"],
  windows_heartbeat_progressing: ["Heartbeat progressing", "good"],
  validating_host_heartbeat: ["Validating heartbeat", "warning"],
  heartbeat_stale: ["Heartbeat stale", "bad"],
  conflicting_host_senders: ["Multiple senders", "bad"],
  windows_maintenance: ["Windows maintenance", "warning"],
  windows_starting: ["Windows starting", "warning"],
  windows_restarting: ["Windows restarting", "warning"],
  windows_stopping: ["Windows stopping", "warning"],
};
const recoveryStates = {
  dry_run: ["Dry-run", "warning"],
  armed: ["Armed", "good"],
  disabled: ["Disabled", "neutral"],
  not_implemented: ["Observe only", "neutral"],
};
const networkStates = {
  icmp_reply: "Ping replied",
  no_icmp_reply: "No ping reply",
  probe_timeout: "Ping timed out",
  probe_error: "Ping probe error",
  not_checked: "Ping pending",
};
const upsStates = {
  utility: ["Online mode", "good"],
  battery: ["On battery", "warning"],
  bypass: ["Bypass", "warning"],
  boost: ["AC input · boost", "warning"],
  trim: ["AC input · trim", "warning"],
  off: ["Output off", "bad"],
  unconfigured: ["Setup pending", "neutral"],
};
const upsActions = {
  shutdown_wait: "Battery shutdown countdown",
  shutdown_checking: "Checking Windows and UPS",
  shutdown_reserved: "Shutdown requested",
  shutdown_sent: "Shutdown acknowledged",
  shutdown_unconfirmed: "Shutdown unconfirmed",
  power_on_wait: "Power-on countdown",
  power_on_checking: "Checking PiKVM and UPS",
  power_on_reserved: "Power on requested",
  power_on_sent: "Power on acknowledged",
  power_on_unconfirmed: "Power on unconfirmed",
  maintenance_hold: "Waiting for maintenance to end",
  record_error: "UPS recovery state unavailable",
};
const upsReasons = {
  snmp_no_response: "UPS did not respond",
  snmp_authentication: "UPS authentication failed",
  snmp_python_missing: "UPS Python environment missing",
  snmp_dependency_missing: "SNMP dependency missing",
  snmp_unsupported_source: "UPS power source unavailable",
  snmp_error: "UPS query failed",
  ups_sample_stale: "UPS reading stale",
  ups_credentials_permissions: "Check UPS credential file permissions",
  ups_credentials_invalid: "Check UPS credentials",
  pikvm_unavailable: "PiKVM unavailable for power on",
  already_powered: "Server power LED is already on",
  windows_ssh_unavailable: "Windows SSH check failed",
  cancelled_before_send: "Power request paused before sending",
  record_error: "UPS recovery state unavailable",
};

function renderUPS(ups) {
  const [headline, tone] = upsStates[ups.state] ?? ["Unknown", "warning"];
  let action = upsActions[ups.action];
  if (["shutdown_wait", "power_on_wait"].includes(ups.action) && Number.isInteger(ups.seconds_remaining)) {
    const minutes = Math.floor(ups.seconds_remaining / 60);
    const seconds = String(ups.seconds_remaining % 60).padStart(2, "0");
    const label = ups.action === "shutdown_wait" ? "Soft shutdown" : "Power on";
    action = ups.seconds_remaining > 0 ? label + " in " + minutes + ":" + seconds : label + " due";
  }
  const source = ups.state === "utility" ? "Running on AC input" : "";
  showCard("ups", headline, [source, action, upsReasons[ups.action_reason ?? ups.reason]].filter(Boolean).join(" · "), tone);
}

function showCard(id, headline, detail, tone) {
  element(id + "-card").dataset.tone = tone;
  text(id, headline);
  text(id + "-detail", detail);
}

const observerStates = new Map([
  ["reporting", ["Reporting", "good"]],
  ["missing", ["Waiting for observer", "warning"]],
  ["stale", ["Telemetry stale", "bad"]],
  ["unreadable", ["Status unreadable", "bad"]],
  ["windows_heartbeat_unavailable", ["Windows heartbeat unavailable", "warning"]],
]);
const roleTones = new Map([
  ["Healthy", "good"], ["Degraded", "warning"], ["Heartbeat stale", "bad"],
  ["Unhealthy", "bad"], ["Duplicate role sources", "bad"], ["Awaiting heartbeat", "neutral"],
]);
const mapperStates = new Map([
  ["running", ["Process running", "good"]], ["not_running", ["Process stopped", "bad"]],
]);
let lastShowEvents = "";

function renderShow(snapshot) {
  const show = snapshot.state === "running" ? snapshot.show ?? {} : {};
  const [observer, tone] = observerStates.get(show.state) ?? ["Waiting for telemetry", "neutral"];
  const reporting = show.state === "reporting";
  showCard("observer", observer, reporting ? `Sample age ${show.age_seconds} s` : "", tone);
  for (const [id, name] of [["td-a", "TD_A"], ["td-b", "TD_B"]]) {
    const role = reporting ? show.roles?.[name] ?? {} : {};
    const state = role.state || "—";
    const details = [role.fault, role.detail];
    if (Number.isFinite(role.age_seconds)) details.unshift(`Heartbeat ${role.age_seconds} s`);
    if ((state === "Healthy" || state === "Degraded") && role.process_match !== "matched") {
      showCard(id, "Process match unavailable", details.filter(Boolean).join(" · "), "warning");
    } else if (Number.isFinite(role.age_seconds) && role.age_seconds >= 5) {
      showCard(id, "Heartbeat stale", details.filter(Boolean).join(" · "), "bad");
    } else {
      showCard(id, state, details.filter(Boolean).join(" · "), roleTones.get(state) ?? "neutral");
    }
  }
  const [mapper, mapperTone] = mapperStates.get(show.madmapper) ?? ["—", "neutral"];
  showCard("mapper", mapper, show.madmapper === "running" ? "Windows process check" : "", mapperTone);
  const metrics = element("observer-metrics");
  metrics.hidden = !reporting || !Number.isInteger(show.accepted_packets) || !Number.isInteger(show.rejected_packets);
  if (!metrics.hidden) {
    text("observer-metrics", `${show.accepted_packets.toLocaleString()} TD packets accepted · ${show.rejected_packets.toLocaleString()} rejected`);
  }

  const events = snapshot.show_events;
  if (!Array.isArray(events)) return;
  const signature = JSON.stringify(events);
  if (signature === lastShowEvents) return;
  lastShowEvents = signature;
  const list = element("show-events");
  const rows = events.map(event => {
    const row = document.createElement("li");
    const time = document.createElement("time");
    const date = new Date(event.at_unix * 1000);
    time.dateTime = date.toISOString();
    time.textContent = date.toLocaleTimeString([], { hour12: false });
    const message = document.createElement("span");
    let label = event.name === "Observer" ? observerStates.get(event.state)?.[0]
      : event.name === "MadMapper" ? mapperStates.get(event.state)?.[0] : event.state;
    if ((label === "Healthy" || label === "Degraded") && event.process_match !== "matched") {
      label = "Process match unavailable";
    }
    const details = event.name === "UPS"
      ? ["UPS", upsStates[event.state]?.[0] ?? "Unknown", upsActions[event.fault], upsReasons[event.detail]]
      : [event.name, label ?? event.state, event.fault, event.detail];
    message.textContent = details
      .filter(Boolean).join(" · ");
    row.append(time, message);
    return row;
  });
  list.replaceChildren(...rows);
  list.hidden = !rows.length;
  element("events-empty").hidden = !!rows.length;
}

function render(snapshot) {
  const [headline, explanation, tone] = watchdogStates[snapshot.state] ?? watchdogStates.unreadable;
  element("primary").dataset.tone = tone;
  text("watchdog", headline);
  const staleSeconds = Math.floor(snapshot.age_seconds);
  text("watchdog-detail", snapshot.state === "stale" && Number.isFinite(staleSeconds) && staleSeconds >= 10
    ? "No watchdog update for " + staleSeconds.toLocaleString() + " seconds."
    : explanation);
  document.title = headline + " | Oculus";

  const updated = new Date(Number.isFinite(snapshot.updated_unix) ? snapshot.updated_unix * 1000 : NaN);
  if (Number.isFinite(updated.getTime())) {
    element("updated").dateTime = updated.toISOString();
    text("updated", updated.toLocaleTimeString());
  } else {
    element("updated").removeAttribute("datetime");
    text("updated", "—");
  }
  text("version", snapshot.version ? "Watchdog v" + snapshot.version : "");

  updatePowerControls(snapshot.power_controls, snapshot.recovery_controls);
  renderShow(snapshot);

  if (snapshot.state !== "running") {
    for (const name of ["windows", "pikvm", "ups", "recovery"]) {
      showCard(name, "—", "Waiting for fresh watchdog status", "neutral");
    }
    return;
  }

  const windows = snapshot.windows ?? {};
  const network = networkStates[snapshot.network?.state];
  const [windowsTitle, windowsTone] = windowsStates[windows.state] ?? ["Unknown", "neutral"];
  const windowsDetail = [
    Number.isInteger(windows.sequence) ? "Sequence " + windows.sequence : "",
    network ?? "",
  ].filter(Boolean).join(" · ");
  showCard("windows", windowsTitle, windowsDetail || "No network reading", windowsTone);

  const pikvm = snapshot.pikvm ?? {};
  const pikvmState = {
    reachable: ["Reachable", "good"],
    stale: ["Status stale", "warning"],
    unavailable: ["Unavailable", "bad"],
    pending: ["Connecting", "neutral"],
    disabled: ["Disabled", "neutral"],
  };
  const [pikvmTitle, pikvmTone] = pikvmState[pikvm.state] ?? ["Unknown", "neutral"];
  const power = pikvm.power_led === true ? "Power LED on" : pikvm.power_led === false ? "Power LED off" : "";
  const atx = pikvm.enabled === false ? "ATX disabled" : pikvm.busy === true ? "ATX busy" : power;
  const reason = typeof pikvm.reason === "string" ? pikvm.reason.replaceAll("_", " ") : "";
  const pikvmDetail = [pikvm.ip, pikvm.state === "reachable" ? atx : reason]
    .filter(Boolean).join(" · ");
  showCard("pikvm", pikvmTitle, pikvmDetail || "Waiting for ATX status", pikvmTone);
  renderUPS(snapshot.ups ?? {state: "unconfigured"});

  const decision = snapshot.recovery?.state;
  const resetDisabled = snapshot.recovery?.network_reset_enabled === false;
  const paused = snapshot.automatic_restart === "armed" && (snapshot.maintenance === true
    || snapshot.recovery?.ups_hold === true
    || decision === "windows_boot_grace" || decision === "windows_boot_delayed");
  const [recoveryTitle, recoveryTone] = resetDisabled ? ["Observe only", "neutral"]
    : paused ? ["Paused", "warning"]
    : recoveryStates[snapshot.automatic_restart] ?? ["Unknown", "neutral"];
  const maintenance = snapshot.maintenance === true ? "Maintenance on"
    : snapshot.maintenance === false ? "Maintenance off" : "Maintenance unknown";
  const guard = snapshot.recovery?.restart_guard_ready === true ? "Restart guard reporting"
    : snapshot.recovery?.restart_guard_ready === false ? "Waiting for restart guard" : "";
  const grace = snapshot.recovery?.boot_grace_seconds;
  const countdown = snapshot.recovery?.reset_in_seconds;
  const remaining = snapshot.recovery?.outage_seconds_remaining;
  const lastResult = snapshot.recovery?.last_reset_result;
  const status = snapshot.recovery?.ups_hold ? "UPS power cycle in progress"
    : decision === "windows_boot_grace" && Number.isInteger(grace) ? `Windows boot grace ${grace} s`
    : decision === "windows_boot_delayed" ? "Windows boot overdue · reset paused"
    : decision === "contact_loss_unverified" ? "Windows unreachable · hard reset withheld"
    : decision === "confirming_outage" && Number.isInteger(remaining) ? `Confirming contact loss · ${remaining} s left`
    : decision === "reset_interval_wait" && Number.isInteger(countdown) ? `${lastResult === "reset_acknowledged" ? "Reset sent" : "Reset unconfirmed"} · next attempt in ${countdown} s`
    : decision === "reset_preflight_wait" || decision === "reset_retry_wait" ? "PiKVM reset blocked · rechecking"
    : decision === "windows_guard_reporting" ? "Windows guard is still responding"
    : decision === "waiting_for_healthy_baseline" ? "Waiting for verified Windows contact"
    : decision === "reset_budget_error" ? "Reset state could not be saved"
    : snapshot.maintenance === false && typeof decision === "string" && decision !== "waiting_for_restart_guard"
      ? decision.replaceAll("_", " ") : "";
  const recoveryDetail = [maintenance, status, guard].filter(Boolean).join(" · ");
  showCard("recovery", recoveryTitle, recoveryDetail, snapshot.maintenance === true ? "warning" : recoveryTone);
}

async function refresh() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2300);
  try {
    const response = await fetch("/api/status", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error("Status request failed");
    render(await response.json());
  } catch {
    render({ state: "disconnected" });
  } finally {
    clearTimeout(timeout);
    setTimeout(refresh, 1000);
  }
}

const fullscreen = element("fullscreen");
if (document.fullscreenEnabled && document.documentElement.requestFullscreen) {
  fullscreen.hidden = false;
  fullscreen.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch {
      fullscreen.hidden = true;
    }
  });
  document.addEventListener("fullscreenchange", () => {
    text("fullscreen", document.fullscreenElement ? "Exit full screen" : "Full screen");
  });
}

const powerButtons = [...document.querySelectorAll(".power-actions button")];
const powerDialog = element("power-confirm");
const recoverySwitch = element("recovery-toggle");
const powerFragment = new URLSearchParams(location.hash.slice(1)).get("power");
let powerToken;
try {
  if (powerFragment && /^[A-Za-z0-9_-]{43}$/.test(powerFragment)) {
    sessionStorage.setItem("oculus-power", powerFragment);
  }
  powerToken = sessionStorage.getItem("oculus-power");
} catch {
  powerToken = powerFragment;
}
if (powerFragment) history.replaceState(null, "", location.pathname + location.search);
let powerState;
let recoveryState;
let powerBusy = false;
let pendingAction;

function updatePowerControls(state, recovery) {
  powerState = state;
  recoveryState = recovery;
  for (const button of powerButtons) {
    const action = button.dataset.action;
    const channel = action === "power-on" ? "power_on" : action.startsWith("hard-") ? "hard" : "soft";
    button.disabled = !powerToken || powerBusy || state?.[channel] !== true;
  }
  const reason = !powerToken ? "Open the dashboard using Open-Power-Display.command to access power controls."
    : !state ? "Power controls are unavailable while the dashboard reconnects."
    : state.reason || "Select an action for the Windows show server.";
  text("power-readiness", reason);
  const enabled = recovery?.enabled;
  recoverySwitch.disabled = !powerToken || powerBusy || recovery?.can_change !== true;
  recoverySwitch.setAttribute("aria-checked", String(enabled === true));
  text("recovery-toggle-state", enabled === true ? "On" : enabled === false ? "Off" : "—");
}

function actionResult(id, message, tone) {
  const result = element(id);
  result.hidden = false;
  result.dataset.tone = tone;
  text(id, message);
}

async function sendPower(action) {
  const recoveryAction = action.startsWith("recovery-");
  const resultId = recoveryAction ? "recovery-result" : "power-result";
  powerBusy = true;
  updatePowerControls(powerState, recoveryState);
  actionResult(resultId, recoveryAction ? "Updating auto-recover…" : "Sending power request…", "neutral");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 38000);
  try {
    const response = await fetch("/api/power", {
      method: "POST", signal: controller.signal,
      headers: {"Content-Type": "application/json", "X-Oculus-Power-Token": powerToken},
      body: JSON.stringify({action}),
    });
    const {message} = await response.json();
    if (response.status === 403) {
      powerToken = null;
      try { sessionStorage.removeItem("oculus-power"); } catch {}
      actionResult(resultId, "Reopen the dashboard using Open-Power-Display.command.", "warning");
    } else {
      actionResult(resultId, message, response.ok ? "good" : "warning");
    }
  } catch {
    const message = recoveryAction ? "The dashboard did not confirm the change. Check the switch after the next status update."
      : "The dashboard did not confirm the request. Check the server before retrying.";
    actionResult(resultId, message, "warning");
  } finally {
    clearTimeout(timeout);
    powerBusy = false;
    updatePowerControls(undefined, recoveryState && {...recoveryState, can_change: false});
  }
}

for (const button of powerButtons) {
  button.addEventListener("click", () => {
    pendingAction = button.dataset.action;
    powerDialog.returnValue = "cancel";
    const poweringOn = pendingAction === "power-on";
    text("power-question", poweringOn ? "Power on the show server?" : button.textContent + " the show server?");
    const description = poweringOn ? "PiKVM will request that the server start."
      : pendingAction.startsWith("hard-") ? "PiKVM will act immediately. Unsaved work may be lost."
      : "Windows will close applications normally. An app with unsaved work may block the request.";
    text("power-explanation", description + " The watchdog will enter maintenance.");
    powerDialog.showModal();
  });
}

recoverySwitch.addEventListener("click", () => {
  if (recoverySwitch.disabled) return;
  if (recoveryState.enabled === true) {
    sendPower("recovery-off");
    return;
  }
  pendingAction = "recovery-on";
  powerDialog.returnValue = "cancel";
  text("power-question", "Enable auto-recover?");
  text("power-explanation", "After 60 seconds without signed Windows contact, the watchdog sends a PiKVM hard reset and retries every five minutes until Windows responds. Enabling this clears maintenance after the startup checks pass.");
  powerDialog.showModal();
});

powerDialog.addEventListener("close", () => {
  const action = pendingAction;
  pendingAction = undefined;
  if (action && powerDialog.returnValue === "confirm" && !powerBusy) sendPower(action);
});

refresh();
