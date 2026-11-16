SF MoMA Oculus Watchdog Telemetry
Mac host monitor 0.2.6

Eaton UPS upgrade on an existing Mac

Keep the installed network configuration and run the included monitor tests
before replacing the background monitor and dashboard. The existing Windows
SSH power handler and PiKVM ATX connection provide the two server actions.

   cd "$HOME/Downloads/Oculus_Watchdog_Telemetry" && cp "/Library/Application Support/SFMoMAOculus/HostMonitor/monitor.config.json" ./Mac/HostMonitor/monitor.config.json && /bin/bash ./Mac/HostMonitor/Test-Monitor.command

   /bin/bash ./Mac/HostMonitor/Remove-Background.command && /bin/bash ./Mac/HostMonitor/Install-Background.command && /bin/bash ./Mac/WatchdogDisplay/Install-Display.command

Setup uses PySNMP and cryptography in the sfmoma account's existing
ups-venv. Create that environment if needed:

   ups_env="$HOME/Library/Application Support/SFMoMAOculus/ups-venv"; /Library/Developer/CommandLineTools/usr/bin/python3 -m venv "$ups_env" && "$ups_env/bin/python" -m pip install --only-binary=:all: 'pysnmp>=7.1.16,<8' 'cryptography>=43'

Substitute the UPS IPv4 address in the next command. Enter the active
read-only SNMPv3 username and the SHA-256 password and AES key privately.
The command saves credentials only after a successful read:

   /bin/bash ./Mac/HostMonitor/Run-Monitor.command --configure-ups UPS_IPV4

The dashboard shows Online mode when the UPS is running on AC input.
The dashboard starts updating within the poll interval. The UPS shutdown
timer runs for ten continuous minutes of observed battery power. AC return
or an unknown sample clears it. After the watchdog receives Windows'
shutdown acknowledgement, three continuous minutes of observed AC are
required for an automatic startup. The countdown restarts on battery or
an unknown sample. The final startup check requires PiKVM ATX to be ready
with the server power LED off.

Mac maintenance pauses new UPS actions. The monitor holds automatic hard
recovery during an active UPS power cycle and clears the hold after host
heartbeat and Restart Guard return on stable utility. The watchdog records
write attempts on disk to avoid repeating ambiguous requests. A failed or
unconfirmed action displays in the UPS card and event log. Power status
unknown is distinct from both confirmed battery and confirmed AC.

Verify the Windows read-only SSH status and installed PiKVM power wiring
before testing a live outage and restoration at the venue.

Upgrade an existing installation

1. On the Mac mini, extract into Downloads. Preserve its live configuration,
   switch to dry-run and maintenance, then reload the monitor and display.

   cd "$HOME/Downloads/Oculus_Watchdog_Telemetry" && cp "/Library/Application Support/SFMoMAOculus/HostMonitor/monitor.config.json" ./Mac/HostMonitor/monitor.config.json && /bin/bash ./Mac/HostMonitor/Run-Monitor.command --maintenance on && /bin/bash ./Mac/HostMonitor/Run-Monitor.command --recovery dry-run && /bin/bash ./Mac/HostMonitor/Remove-Background.command && /bin/bash ./Mac/HostMonitor/Install-Background.command && /bin/bash ./Mac/WatchdogDisplay/Install-Display.command

2. On CP007-01, extract the same package into Downloads and run this from
   elevated PowerShell under the show account. It uses the installed
   heartbeat key and installs the automatic Windows Restart Guard service.

   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Oculus_Watchdog_Telemetry\Windows\Install-OculusRestartGuard.ps1"

   The Mac dashboard should show Restart guard reporting. Keep dry-run for
   the first real desktop restart test. When the show can stop, end
   maintenance while keeping dry-run active:

   /bin/bash "$HOME/Downloads/Oculus_Watchdog_Telemetry/Mac/HostMonitor/Run-Monitor.command" --maintenance off

   Restart from Windows Start > Power > Restart. Watch the Mac show
   Windows boot grace and then return to observing after the Windows
   heartbeat progresses again. Allow a few seconds for guard status.
   Confirm Restart Guard reporting after Windows returns. Then enable
   Auto-recover in the power dashboard, or run this on the Mac:

   cd "$HOME/Downloads/Oculus_Watchdog_Telemetry" && /bin/bash ./Mac/HostMonitor/Run-Monitor.command --recovery arm && /bin/bash ./Mac/HostMonitor/Run-Monitor.command --maintenance off

   Expect Auto-recover On, Maintenance off and Restart guard reporting.
   Fresh settings default to on. Installation preserves saved recovery and
   maintenance settings, including the dry-run used above to stage an upgrade.

After both signed Windows sources have been healthy, the Mac requests a
PiKVM hard reset if both go silent for 60 seconds. It retries after five
minutes while both remain silent and PiKVM ATX reports ready and powered on.
The dashboard shows the reset and next attempt countdown. Windows contact
inhibits another attempt. An ICMP reply may continue during a hard freeze.
Loss of the Windows network path while PiKVM remains reachable can also
look like a freeze.

A Windows preshutdown notice or an observed power-on pauses automatic
recovery until Windows returns. The dashboard marks a protected boot delayed
after five minutes. The Windows host heartbeat and application observer
continue to report separately. A cancelled desktop restart can clear the
hold when the original source and guard renew contact for 20 seconds.

Windows host recovery and application telemetry

Check native Windows restart after a system crash from PowerShell:

   Get-CimInstance Win32_OSRecoveryConfiguration | Select-Object AutoReboot

AutoReboot=True allows Windows to restart after a bugcheck. For a hard
hang without a bugcheck, use the Mac/PiKVM host recovery policy above.
After a test restart, confirm the signed host heartbeat and Restart Guard
resume. In the show session, confirm the observer reports the expected TD
heartbeats and MadMapper process. The TouchDesigner developer owns TD
relaunch.

TouchDesigner/Oculus_TD_A_Execute_DAT.py reports when either Spout sender
stops cooking. Set EXPECT_CONTINUOUS_SPOUT_COOKS only where the approved
project expects continuous updates. Held frames may be deliberate. Verify
MadMapper and the projected image independently.

Telemetry installation for a new deployment

The Mac dashboard shows Windows observer freshness, TD_A and TD_B status,
heartbeat ages and fault details, MadMapper process presence, observer
packet counts, and the 12 most recent application state changes on the Mac.
The Windows sender reads the observer snapshot from the show user's profile.
It uses the existing signed UDP 19741 heartbeat and shared key.

Use the current Windows observer and DATs. Keep the watchdog in dry-run
while commissioning. This update runs in the watchdog processes and can
be installed with show content playing. Installing it briefly reloads the
Mac watchdog, the Mac display and the Windows heartbeat task.

1. Update the Mac first

Extract the entire folder into Downloads on the Mac mini. In Terminal as
sfmoma, run the backup and tests before reloading the watchdog services.
Keep the previous Mac monitor and display packages for rollback.

   cd "$HOME/Downloads/Oculus_Watchdog_Telemetry" && cp "/Library/Application Support/SFMoMAOculus/HostMonitor/monitor.config.json" ./Mac/HostMonitor/monitor.config.json && /bin/bash ./Mac/HostMonitor/Test-Monitor.command

   cd "$HOME/Downloads/Oculus_Watchdog_Telemetry" && /bin/bash ./Mac/HostMonitor/Remove-Background.command && /bin/bash ./Mac/HostMonitor/Install-Background.command && /bin/bash ./Mac/WatchdogDisplay/Install-Display.command

   /bin/bash "$HOME/Downloads/Oculus_Watchdog_Telemetry/Mac/HostMonitor/Show-Status.command"

Expect version 0.2.6, a fresh snapshot, and the existing Windows host
heartbeat. With the original Windows sender still installed, the observer
card reads Waiting for telemetry. The Mac continues accepting its heartbeat.

2. Update the Windows sender

Extract the package into Downloads on CP007-01. Use an elevated
PowerShell window on the existing show account. Run this self-test first.

   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Oculus_Watchdog_Telemetry\Windows\Send-OculusHostHeartbeat.ps1" -SelfTest

Expect PASS before proceeding. Replace the sender file used by the existing
task. This preserves the original sender alongside it for rollback.

   $ErrorActionPreference='Stop'; $src=Join-Path $env:USERPROFILE 'Downloads\Oculus_Watchdog_Telemetry\Windows\Send-OculusHostHeartbeat.ps1'; $dst=Join-Path $env:LOCALAPPDATA 'SFMoMAOculus\HostBeacon\Send-OculusHostHeartbeat.ps1'; $task=Get-ScheduledTask -TaskName 'Oculus Host Heartbeat' -TaskPath '\'; if ((-not (Test-Path -LiteralPath $dst -PathType Leaf)) -or (-not ($task.Actions.Arguments -like "*$dst*"))) { throw 'The installed heartbeat task points to a different sender.' }; if (-not (Test-Path -LiteralPath "$dst.pre-telemetry")) { Copy-Item -LiteralPath $dst -Destination "$dst.pre-telemetry" }; Copy-Item -LiteralPath $src -Destination $dst -Force

Restart only the Oculus Host Heartbeat task. The observer continues to
run in the user's existing Windows session.

   Stop-ScheduledTask -TaskName 'Oculus Host Heartbeat' -TaskPath '\'

   Start-ScheduledTask -TaskName 'Oculus Host Heartbeat' -TaskPath '\'

3. Verify the Mac dashboard

Install the Windows Restart Guard under step 2 of Upgrade an existing
installation before commissioning Auto-recover.

With the Windows observer status fresh, check for Reporting and the
current TD_A state. Expect TD_A Healthy when the two local Spout checks
pass, TD_B Awaiting heartbeat while it is offline, and MadMapper Process
running. The packet counters should agree with the Windows observer.
The recent-events readout records the application status changes seen
since install. The Windows observer log retains the earlier history.

Automatic recovery uses the signed Windows host and Restart Guard streams. Application telemetry and the displayed events are
observational. MadMapper process presence tracks the process. Use the
physical projection check to verify the two bridge output paths.

To roll back, restore the .pre-telemetry Windows sender and restart the
Oculus Host Heartbeat task first. Then reinstall the previous Mac monitor
and display packages. This order keeps Windows packets compatible with
the Mac receiver. State, logs, keys and PiKVM credentials remain in their
existing state directories.

Validation
The Python tests cover the signed restart notice, restart and power-on grace,
the 60-second outage threshold, five-minute repeat interval and
recovery after both authenticated Windows sources return. An independent loopback test passed for signed Windows-format
packets through the Mac monitor and dashboard API, including application
fault and stale telemetry. The JavaScript render paths were checked with
a simulated DOM. Run the included Windows PowerShell self-test before
updating the scheduled task. Live macOS and Windows installation are
still to be verified at the venue.
