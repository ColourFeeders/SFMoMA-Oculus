SF MoMA Oculus Windows application observer v0.2.1

The scheduled observer publishes status at the configured one-second cadence
and refreshes the same-session process inventory every five seconds by default.
At that cadence it performs roughly 12 process scans per minute, down from 60.
MadMapper process presence and TouchDesigner PID correlation can take several
seconds to reflect a change. TouchDesigner heartbeat age and health are computed
on every status update.

The observer skips window-title queries. The Windows JSON still carries a
window_title field with a null value. This removes title-bar captions from
the inventory and logs. A title may include an open MadMapper project name.
TouchDesigner provides its .toe path in its own heartbeat. For a one-time
title check from the show desktop, run:

   Get-Process -Name TouchDesigner,TouchPlayer,MadMapper -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,MainWindowTitle

Install the console-free task action

Extract Oculus_Observer_Low_Overhead into Downloads on CP007-01. When the
server is available, run this in an elevated PowerShell under the show account:

   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Oculus_Observer_Low_Overhead\Update-ObserverTask.ps1"

Run the installer from an existing elevated PowerShell on the control
display. The updater builds a Windows GUI executable that starts PowerShell
with no console. It runs the bundled observer self-test through that
launcher before switching the task action. The existing logon trigger,
user session, configuration and logs are preserved. The installer starts
the updated task and waits for new status.

For subsequent manual starts, run:

   Start-ScheduledTask -TaskName 'Oculus Application Observer' -TaskPath '\'

Both a manual start and sign-in use the console-free launcher. Launcher
errors are recorded in observer-launch-error.txt under
%LOCALAPPDATA%\SFMoMAOculus\WatchdogObserver.

Confirm Windows observer shows Reporting on the Mac, and watch TD_A during
playback. On Windows, check the version and snapshot timestamp:

   Get-Content -LiteralPath "$env:LOCALAPPDATA\SFMoMAOculus\WatchdogObserver\observer-status.json" -Raw | ConvertFrom-Json | Select-Object observer_version,generated_at_utc,accepted_packets,rejected_packets

With the same Splashtop session and show workload, repeat a representative
remote action and compare responsiveness. The reduced work is a code-level
change. Its effect on Splashtop needs this comparison at the venue.

Rollback

   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Oculus_Observer_Low_Overhead\Update-ObserverTask.ps1" -Restore

This starts the previous observer script with the same console-free launcher.

Validation

The heartbeat parser, health calculation, status writer, and socket binding
were compared against v0.2.0. The Mac sender consumes the same status fields.
The updater performs the Windows PowerShell self-test at the venue before
changing the task action. Verify the first manual start on the show desktop
and confirm the displayed content stays in front.
