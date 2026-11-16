SF MoMA Oculus - Windows application observer v0.2.0

PURPOSE
The observer receives TD Execute DAT heartbeats and inventories running
TouchDesigner, TouchPlayer and MadMapper processes in its Windows session.
It uses the current PID and process start time to correlate a fresh TD
heartbeat with a running process. Project and asset paths need no configuration.
Keep using the existing Execute DAT and UDP port 19740.

It has no application launch, termination, MadMapper control, reboot, or
scheduled-task installation code. A mode other than "observe" is rejected.

RUN ON THE SHOW SERVER
1. Extract the complete ZIP into a new folder.
2. Run Test-Observer.cmd. The built-in checks should finish with PASS.
3. Close the previous Windows observer window to free UDP port 19740.
4. Run Start-Observer.cmd in the new folder from the exhibition user's desktop.
   Administrator rights and an extra Python installation are not needed.
5. Leave its console open while testing the TD heartbeat.
6. Press Ctrl+C or close the window to stop observation.

An equivalent PowerShell command from the extracted folder is:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Observe-OculusHeartbeat.ps1

To register the observer at show-account sign-in, run
Install-OculusObserverAtLogon.ps1 from the show desktop. Close any manual
observer before running the new task from Task Scheduler.

EXPECTED WITH THE CURRENT TEST SHOW
- TD_A: Awaiting health integration, process=matched, with the current PID.
  The default get_health() proves the heartbeat path and reports its placeholder.
- TD_B: Awaiting heartbeat while there is no TD_B sender.
- MadMapper process: running, with its current PID. This is process presence.
  Fullscreen output titles are permitted and do not determine project identity.

On the test show, switch Active off on the TD_A watchdog DAT for at least
6 seconds and back on. The TD_A role should go stale and then return to its
current reported health state. The application inventory should still list
TouchDesigner and MadMapper during the test.

TD DEVELOPER INTEGRATION
Use the previously supplied Oculus_Watchdog_Execute_DAT.py in one Execute DAT
per project. Set INSTANCE to TD_A (playback/compositor) or TD_B (interactive).
Enable Active, Start, Frame End and Exit; leave other callbacks disabled.
Keep the destination set to 127.0.0.1 UDP port 19740, matching this receiver.
Replace get_health() with fast checks of the actual required show pipeline.

ready: initialization complete and this instance can perform its role.
output_valid: the required output passes the project checks now.
degraded: useful output remains, but some functionality is unavailable.
fault/detail: short, deliberate codes/messages explaining a failed check.

For TD_A, missing TD_B can mean ready=true, output_valid=true,
degraded=true, fault="INTERACTION_UNAVAILABLE" if base playback is healthy.
No visitors, intentional stills and normal looping content are healthy states.

WHAT THE OBSERVER CHECKS
- Correct v1 heartbeat schema, known role, PID, project and stream identity.
- Actual boolean health values and bounded fault messages.
- Strictly increasing sequence numbers within the retained source history;
  duplicates do not renew liveness.
- Freshness using the receiver's own monotonic clock.
- About 10 seconds of sustained readiness before displaying Healthy/Degraded.
- Conflicting fresh sources claiming the same role.

The process_match field is separate from the TD health state. A match checks
the reported PID against a TD process in the observer's session that was
started before the last heartbeat was received. The PID, project and launch
token remain sender claims, so identity_verified stays false.
Recompiling a DAT or changing the reported project can briefly show duplicate
sources until the previous stream goes stale (normally 5 seconds).
Inactive source records and their sequence history expire after 60 seconds by
default. A returning source must establish readiness again. This observer is
not an authenticated or process-verified basis for automatic recovery.

OUTPUT
Logs and the latest status JSON are stored under:
  %LOCALAPPDATA%\SFMoMAOculus\WatchdogObserver
Use observer.config.json to change the log directory or test thresholds.
observer-status.json includes both TD role states and an applications object
with the session, process inventory and MadMapper presence. Inventory state
can be ok, partial or unavailable. Logs record state changes and periodic
summaries, with size/count rotation. They contain local paths, window titles,
PIDs and fault text.

SCOPE OF THIS TEST
TD health values come from the DAT. MadMapper presence reports a process in
the show session. Spout reception, projector output and GPU/Sync health
require separate checks. Closing the observer stops observation. Run the
packaged self-test on Windows PowerShell 5.1 before starting the live test.
