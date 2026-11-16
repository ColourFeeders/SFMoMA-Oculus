SF MoMA Oculus - Windows heartbeat startup

The startup task sends the existing signed OS heartbeat from CP007-01 to the
Mac mini (192.0.2.135, UDP 19741). Its boot trigger starts 30 seconds after
Windows starts. It runs as the show account without requiring desktop login,
runs indefinitely, ignores a duplicate start of the same task, and retries
a failed process up to three times at one-minute intervals. It sends phase
running so the Mac can establish its healthy heartbeat baseline.

WHEN THE SHOW SERVER IS AVAILABLE

1. On the Mac, check that the dashboard shows Watchdog running, PiKVM
   reachable, Dry-run and Maintenance on. Check those settings before
   commissioning the Windows beacon or performing the reset test.

2. On the Windows server, sign in as the account that owns the existing
   beacon folder. The default is:
     %LOCALAPPDATA%\SFMoMAOculus\HostBeacon

   That folder needs the same host-heartbeat.key already shared with the Mac.
   The ZIP includes the known sender script. The installer copies it if the
   folder has no sender and preserves the existing key. If you moved the
   beacon folder, supply -BeaconDirectory 'C:\path to\HostBeacon'.

   Open PowerShell as administrator using the same account and run the
   installer. Adjust the path if the ZIP was extracted elsewhere:

     powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Oculus_Host_Heartbeat_Startup\Install-OculusHeartbeatStartup.ps1"

   Enter the Windows account password when prompted. The task runs with
   that account's normal permissions. If the account cannot register a boot
   trigger, or Windows policy denies batch logon, have IT register it for
   the show account. The installer leaves an existing task of the same name
   in place.

3. Stop the earlier manually launched beacon if it is still running. Start
   the registered task from Task Scheduler with Run, or from PowerShell:

     Start-ScheduledTask -TaskName 'Oculus Host Heartbeat' -TaskPath '\'

   Give it about 15 seconds. On the Mac, look for Heartbeat progressing
   with a rising sequence. On Windows, Task Scheduler should show Running.
   If Windows shows Running but the Mac has no heartbeat, check for UDP
   19741 delivery, a matching key and synchronized host clocks. Multiple
   senders means a second beacon is still active.

4. In a maintenance window, keep the Mac in dry-run and test the PiKVM
   hardware reset if this particular reset path has not already been tested.
   Verify the heartbeat returns automatically after the boot delay and the
   show output recovers on its own. Check the Windows boot/logon and app
   startup paths if output does not recover. Confirm before arming recovery.

5. Once the boot/reset test passes, the Mac v0.2 watchdog can be armed with
   Run-Monitor.command --recovery arm. It can act only after Mac maintenance
   is off and its healthy baseline is established again. v0.2 requires
   both heartbeat loss and failed pings for at least 60 seconds. It leaves
   freezes that still answer pings for an operator to reset via PiKVM.

The Windows heartbeat only tracks host progress. TouchDesigner, MadMapper
and projected output still need their own health checks.
