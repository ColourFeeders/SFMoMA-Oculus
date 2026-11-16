# Recovery policy and operational boundaries

This document describes the supplied **Mac host monitor 0.2.6**, **Windows observer 0.2.1**, current Windows host beacon, and **OculusRestartGuard** sources. It describes code behavior, not proof that the same release is installed or accepted at the venue. Read [INSTALLATION.md](INSTALLATION.md) before deployment and [VALIDATION.md](VALIDATION.md) for the validation record.

## What can act on the server

The Windows application observer and TouchDesigner Execute DATs report application status. The TouchDesigner developer owns TD relaunch. A progressing Windows host heartbeat measures host liveness independently of application health.

The Mac host monitor can request a **PiKVM hard reset** after an eligible unannounced host outage. The Mac dashboard also exposes manual soft and hard power actions. When the Eaton UPS is configured, the Mac monitor can issue a Windows soft shutdown and restore server power after a sustained utility return.

## Logical endpoints

These are documentation addresses. Replace them in a private deployment copy; do not use them as venue configuration.

| Path | Endpoint / protocol | Purpose |
| --- | --- | --- |
| TD roles to Windows observer | UDP `127.0.0.1:19740` | Local JSON application claims; schema `oculus.td.heartbeat` v1 |
| Windows host beacon and guard to Mac | UDP `192.0.2.135:19741`, accepted Windows source `192.0.2.136` | Signed host progress, application telemetry and lifecycle notices |
| Mac to Windows | ICMP from `192.0.2.135` to `192.0.2.136` | Network reachability diagnostic |
| Mac to PiKVM | HTTPS `192.0.2.141:443` | ATX status and power API; certificate fingerprint pinned |
| Mac browser to dashboard | HTTP `127.0.0.1:19742` | Loopback status and separately authorized power controls |
| Mac to Windows soft-power handler | SSH `192.0.2.136:22` | Restricted `oculuspower` identity and verified SSH host key |
| Mac to Eaton UPS card | SNMPv3 UDP 161 | Read-only SHA-256 and AES source polling |

The shared host key authenticates Windows host and lifecycle messages using HMAC-SHA256. Local TD packets are not authenticated. Same-session PID correlation is diagnostic and does not authenticate a sender. Keep real keys, PiKVM credentials, pins, SSH trust files, tokens and runtime state outside the repository.

## Automatic hard-reset decision

Auto-recover defaults to armed with fresh settings and a configured PiKVM. Saved Off and maintenance flags persist across an upgrade. The installed configuration field `mode: observe` labels the packet-monitor mode. Use `automatic_restart` and `recovery.state` to inspect actual recovery.

1. The monitor first sees one progressing signed Windows host stream and a fresh signed Restart Guard report. A monitor reload starts from a fresh baseline or recovers an outstanding reset interval from disk.
2. Both sources go silent for **60 seconds**, measured from the last authenticated contact. A reply from either Windows source inhibits an attempt. ICMP is diagnostic and may reply during a Windows freeze.
3. Recovery is armed, maintenance is off and there is no boot hold. Current PiKVM ATX information is at most **10 seconds** old, reachable, enabled, idle and reporting the server powered on.
4. Before sending, the monitor checks live ATX, recovery and maintenance flags, and pending Windows UDP traffic. It durably records the attempt and sends the PiKVM hard reset.
5. The watchdog waits **300 seconds** after any committed request, including an unconfirmed response. It repeats while Windows is silent. The interval survives a monitor restart. A running host and guard end the incident. A definite failure before sending is rechecked with backoff.

Dry-run reports eligibility as `would_reset` without contacting the ATX write endpoint. Invalid maintenance or recovery state suppresses reset. A network failure that blocks both signed streams while PiKVM remains reachable can trigger a hardware reset.

## UPS shutdown and restart

The Mac polls the Eaton every ten seconds. The dashboard labels normal AC operation **Online mode**, matching the Eaton 9PX front panel. The Mac sends a Windows soft shutdown after ten continuous minutes of confirmed battery readings. Before sending it checks Windows SSH privileges and reads the UPS again. A power-source change or an unknown reading clears the countdown.

An acknowledged UPS-triggered shutdown starts a three-minute power-on countdown when AC returns. Battery or unknown readings clear the countdown. At expiry the monitor checks the UPS again and verifies that PiKVM ATX is ready and the server is powered off before sending Power on.

When the UPS supplies the optional duration and input event readings, a reduced battery duration or an input event between polls also resets the affected countdown. The monitor can use normal power-source samples if the optional readings are unavailable.

Maintenance pauses both UPS actions. PiKVM hard recovery is held while an automatic UPS cycle is unresolved. The hold clears after stable AC and authenticated Windows liveness return. A verified cancellation before dispatch can retry after the next checks. A command that may have reached the target requires operator review if it remains unconfirmed. Each command is reserved durably before sending. Manual actions wait for a running UPS command to settle.

## Normal restart and boot protection

The Windows service `OculusRestartGuard` sends signed `ready` status every three seconds. On Service Control Manager **preshutdown**, it sends four signed `stopping` packets, 150 ms apart. An ordinary service stop sends no shutdown notice. Lifecycle packets have their own stream and sequence tracking.

A received stopping notice starts a boot hold. A sampled ATX power LED transition from **off to on** also starts a hold outside an automatic reset incident. A first sample showing power on is not evidence of a transition, but startup without a healthy baseline cannot trigger an automatic reset.

During a recognized restart, reset is withheld until all of these return:

- A stable progressing Windows host heartbeat with a different `(boot_id, stream_id)` source from the previous healthy source.
- A fresh Restart Guard report.

For a cancelled restart on the same Windows source, the guard and host must
both report after the hold begins and contact must stay healthy for 20 seconds.

After **300 seconds**, the state changes from `windows_boot_grace` to `windows_boot_delayed`. **The reset hold continues indefinitely; five minutes is not a forced-reset deadline.** Investigate delayed boot through the KVM view. A Windows fast-startup situation can reuse a boot ID; a new beacon stream also satisfies the different-source requirement.

If every preshutdown notice is lost and the monitor does not observe a power cycle, a normal restart can look like an unannounced outage. The guard is therefore not an unconditional guarantee during network loss. Put recovery into maintenance/dry-run before deliberate servicing or restart when assurance is required. A dashboard manual power action first writes maintenance and waits for the monitor to acknowledge it before sending the power command.

A recognized restart ends any outstanding automatic reset incident. If the Mac monitor restarts without an outstanding incident, it waits for the next healthy baseline. If a reset interval is outstanding, the monitor allows at least 60 seconds for signed contact after reloading, and respects the remaining five-minute interval.

## Timers and retention

| Setting | Supplied value | Meaning |
| --- | --- | --- |
| TD Execute DAT interval | 1 s | Sends only when Frame End callbacks run |
| Observer status interval | 1 s | Writes local role/process status |
| Observer process inventory | 5 s | Same-session process presence may lag |
| TD heartbeat timeout | 5 s | No fresh role stream becomes stale |
| TD readiness stability | 10 s | Required before Healthy or Degraded |
| TD startup grace | 120 s | Unready without explicit fault becomes Startup overdue; explicit faults report Unhealthy immediately |
| TD source retention | 60 s | In-memory sequence history lifetime |
| Windows host-beacon interval | 1 s | Host progress and compact observer telemetry |
| Windows startup-task delay | 30 s | Boot task starts before interactive show login |
| Windows host timeout | 15 s | Signed host stream becomes stale |
| Windows healthy stability | 10 s and at least 2 running packets | Required for progressing host status |
| Host accepted clock skew | +/-60 s | Old or substantially future packets rejected |
| Host source retention | 120 s | In-memory source record retention |
| Guard ready interval / timeout | 3 s / 15 s | Lifecycle readiness contact |
| ICMP interval / process timeout | 5 s / 3 s | Bounded native macOS probe |
| PiKVM poll / maximum ATX age | 5 s / 10 s | Stale ATX data cannot permit a reset |
| Unannounced contact-loss gate | 60 s | Measured from the most recent signed host or guard contact |
| Protected-boot advisory threshold | 300 s | Delayed-boot alert; hold remains active |
| Persistent automatic reset interval | 300 s | Reserved before each attempt in an ongoing outage |
| Mac application telemetry expiry | 6 s | Expires independently of host liveness |
| Dashboard snapshot freshness | 10 s | Running process plus fresh timestamp required |
| Event rotation | 2 MiB with 4 backups per log | Mac events/show-events; equivalent observer default retention |

Some values are configurable and others are code constants. The installed configuration and source revision are authoritative; record deliberate changes with acceptance evidence.

## Recovery states

| State | Meaning / operator response |
| --- | --- |
| `maintenance` | Automatic action inhibited. Leave this on during intentional shutdown and service. |
| `disabled` | No PiKVM recovery integration configured. |
| `waiting_for_healthy_baseline` | No eligible prior host state established since startup/change/inhibition. |
| `waiting_for_restart_guard` | Host progresses, but guard contact is not fresh; resolve service/key/network issue. |
| `observing` | No eligible failure currently detected. This is not physical-image validation. |
| `windows_boot_grace` | Recognized restart or sampled power-on transition; reset withheld. |
| `windows_boot_delayed` | Protected boot passed five minutes; reset still withheld. Inspect KVM. |
| `windows_guard_reporting` | Host heartbeat is stale and the guard remains in contact. Check the Windows beacon. |
| `confirming_outage` | Eligible contact-loss condition exists but 60-second gate is incomplete. |
| `atx_unavailable_or_server_off` | ATX stale, unavailable, disabled, busy or power LED off; no reset. |
| `would_reset` | Dry-run conditions qualify; no physical command sent. |
| `reset_interval_wait` | A reset was attempted and the countdown to the next eligible attempt is running. Consult `last_reset_result` for API acknowledgement. |
| `reset_preflight_wait` | A check blocked the request before sending. The monitor will re-evaluate with backoff. |
| `reset_budget_error` | The persistent reset state could not be updated. Check storage and permissions. |

Transport failures and certificate/authentication errors are also reported. Do not disable identity verification to clear a failure.

## What application health does and does not mean

TD_A's supplied DAT checks two exact sender names and dimensions:

| TOP path | Sender name | Dimensions |
| --- | --- | --- |
| `/SfMOMA/mapping/over_bridge/syphonspoutout1` | `UPPER BRIDGE` | 15360 x 2160 |
| `/SfMOMA/mapping/under_bridge/syphonspoutout1` | `LowerBridge` | 15360 x 2160 |

It checks operator type/existence, error state, Active, exact names/dimensions and `totalCooks >= 1`, using passive reads. It also monitors each local cook counter. Held cooks are reported, and they fail readiness when EXPECT_CONTINUOUS_SPOUT_COOKS is selected for the approved show state. The cook counter does not certify pixel content or downstream output. The observer's **Healthy** label depends on received readiness/output-valid claims and stability, not on a successful `process_match`; inspect that diagnostic separately.

Paused timelines or disabled Frame End callbacks can stop TD heartbeats. Duplicate role streams prevent a unique role status and reset readiness stability. The generic TD_B template deliberately reports `HEALTH_CHECKS_NOT_CONFIGURED` until a real project health function is supplied. MadMapper process presence proves neither correct project nor receiver/fullscreen output. No software status in this package verifies projector geometry, actual pixels, FX4 routing or synchronization.

## Startup ownership and maintenance workflow

The host heartbeat task is a boot task with stored Windows account credentials and a 30-second delay. It runs before the show desktop is signed in. The application observer is an interactive logon task and inventories only its own Windows session. Locking that desktop is different from logging out. Password/account changes can affect the host task. The guard runs as LocalService and reads a protected local copy of the shared heartbeat key.

The Mac monitor is a system LaunchDaemon running as the configured Mac user and survives logout. The dashboard is a user LaunchAgent and opens after Mac login. Closing the dashboard does not stop the monitor.

For planned work:

1. Record current mode and show status. Set maintenance on and recovery to dry-run; wait for a fresh snapshot acknowledging both.
2. Save approved work and use the established venue stop/blanking procedure.
3. Restart Windows normally, or use the commissioned dashboard power action. Observe boot through PiKVM.
4. Verify host progress, guard, observer, correct projects, both receivers, fullscreen/mapping and physical projection.
5. Resume only the accepted recovery mode. CLI `--recovery arm` does not clear maintenance. Dashboard Auto-recover On performs arming checks and then clears maintenance; Off saves dry-run. After a dashboard manual power action, Off then On reruns the checks before resuming.

Arming requires a fresh running same-version monitor snapshot, progressing host, guard reporting and live powered/idle/enabled ATX. Passing these checks does not certify the show output.

## Failure boundaries

- Application faults and a stalled beacon with a responsive Restart Guard are reported independently. The TD developer owns TD relaunch.
- Loss of the Windows network path can resemble a host failure if PiKVM stays reachable. Network isolation and venue failure tests must account for this ambiguity.
- A missing/unreachable PiKVM cannot recover the server. Manual power-on requires PiKVM, ATX wiring and the network/power path to remain available.
- A deliberate shutdown protected by a received notice stays held; the package does not later power the server on automatically.
- An acknowledged reset is not successful recovery. Show startup, display enumeration, mapping and receiver reconnection require separate acceptance.

## Validation evidence

The monitor regression suite and packaging checks are recorded in [VALIDATION.md](VALIDATION.md). Tests use temporary state, loopback UDP and a local HTTPS PiKVM stub.

Coverage includes HMAC/clock/sequence handling, competing host senders, maintenance defaults, bounded logs, application telemetry expiry, local receiver integration, certificate pinning, pre-send reset gate, persistent attempt spacing, five-minute repeats, guard lifecycle, restart hold and delayed-boot behavior. Historical component `Validation.txt` files can show older test counts and are not the current release result.

Windows PowerShell/C# runtime checks, actual task/service installation, macOS launchd operation, live TD/MadMapper behavior, ordinary desktop restart, delayed boot, PiKVM wiring, physical display routing and full show restoration remain venue acceptance tasks. Keep dry-run selected for the initial restart-policy tests.
