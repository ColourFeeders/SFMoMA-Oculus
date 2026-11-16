SF MoMA Oculus - Mac host monitor v0.2.6

TODAY: VERIFY PIKVM FROM THE MAC AND UPGRADE LAUNCHD

The Mac uses its wired address 192.0.2.135 to receive signed host beacons on
UDP 19741 from CP007-01 (192.0.2.136), and to reach the PiKVM at
192.0.2.141 over HTTPS/443. It polls ATX status every five seconds. Polling
reports API access, ATX enabled/busy, and the server's power LED. The ATX
poll continues during maintenance. This upgrade preserves the existing
heartbeat key, status/log directory and launchd job name.

1. At the PiKVM's local root console, print the public certificate fingerprint:

   openssl x509 -in /etc/kvmd/nginx/ssl/server.crt -noout -sha256 -fingerprint

   Use the local console as the source for the fingerprint. The Mac pins the
   exact certificate before transmitting its web login. If the PiKVM's
   certificate is replaced, verify the new fingerprint locally and rerun setup.

2. Extract the ZIP to Downloads, as Oculus_Mac_Host_Monitor_v0.2. As sfmoma on
   the Mac, run the tests and configure PiKVM:

   (cd "$HOME/Downloads/Oculus_Mac_Host_Monitor_v0.2" && /bin/bash ./Test-Monitor.command && /bin/bash ./Configure-PiKVM.command)

   Paste the fingerprint line (or just the fingerprint) and enter the PiKVM
   web username and password. The password prompt is hidden and the credentials
   are stored in a user-owned 0600 file inside the Mac monitor's private state
   directory. Do not put the password in a command or send it in chat.
   This build uses PiKVM Basic authentication with a password-based web account.
   If the PiKVM requires a rotating 2FA code for every API request, this build
   needs a separate authentication integration before it can poll unattended.

   Setup sends one GET /api/atx to the configured PiKVM and prints the result.
   Expect state=reachable and enabled=true. If the ATX harness is wired and
   the server is running, expect power_led=true. It also works before the
   old background job is stopped. The self-tests use loopback and test data.

3. Upgrade the existing launchd job, leaving maintenance on and recovery in
   dry-run mode:

   (cd "$HOME/Downloads/Oculus_Mac_Host_Monitor_v0.2" && /bin/bash ./Run-Monitor.command --maintenance on && /bin/bash ./Run-Monitor.command --recovery dry-run && /bin/bash ./Remove-Background.command && /bin/bash ./Install-Background.command)

   Run this as sfmoma. The installer requests admin access for the system job,
   which runs as sfmoma. The interruption is limited to the Mac monitor.
   If installation fails, correct the reported error and rerun
   Install-Background.command. To roll back, remove this job and reinstall
   from the original v0.1 download folder. The state/key/logs are preserved.
   As before, keep the Mac's wired interface on the approved AV network.

4. After a few seconds:

   /bin/bash "$HOME/Downloads/Oculus_Mac_Host_Monitor_v0.2/Show-Status.command"

   Expect version=0.2.6, status_fresh=true, maintenance=true,
   automatic_restart=dry_run, pikvm.state=reachable, and
   recovery.state=maintenance. An absent Windows beacon today is expected if
   the sender is not running. The PiKVM test is independent of Windows login
   and application status.

WHEN YOU HAVE SHOW SERVER ACCESS

Confirm the Windows signed host beacon starts automatically after a reboot,
runs in phase running for show operation, and produces
windows_heartbeat_progressing on the Mac. The supplied Windows script is the
existing manual sender, which by itself does not start on boot. It continues
to use the existing host-heartbeat.key. The TouchDesigner application observer
and its separate loopback UDP 19740 remain independent.

With the recovery mode still dry_run, validate the physical reset path during
a maintenance window using PiKVM's web UI and confirm Windows, the beacon,
and the show recover. Then, with the system healthy and the Mac maintenance
flag on, arm from the Mac:

   /bin/bash "$HOME/Downloads/Oculus_Mac_Host_Monitor_v0.2/Run-Monitor.command" --recovery arm

Arming requires fresh monitor status, a progressing Windows heartbeat,
a reporting Restart Guard service and a powered, idle ATX connection.
After arming, clear maintenance:

   /bin/bash "$HOME/Downloads/Oculus_Mac_Host_Monitor_v0.2/Run-Monitor.command" --maintenance off

New installations default to Auto-recover On. Existing installations
preserve saved settings. The commissioning command above deliberately
stages the upgrade in dry-run until the two Windows signals are verified.

Use the same launcher with --maintenance on to inhibit recovery during
service. Use --recovery dry-run to disarm. These settings survive restarts.

RECOVERY RULES

Windows recovery starts from a progressing signed host beacon and a
reporting Restart Guard. Sixty seconds without either Windows source
qualifies for a reset when PiKVM ATX is idle, enabled and powered on.
Windows contact ends the silence. A Windows network partition can look
like a freeze even when PiKVM is still reachable. Ping is diagnostic.

The Windows Restart Guard sends a signed preshutdown notice. The Mac
pauses auto-recovery for recognized restarts and observed power-ons until
the Windows heartbeat and guard return. After five minutes the dashboard
marks a protected boot overdue and keeps automatic reset paused.

After a reset request, the watchdog waits five minutes before another
attempt, even if the API response was inconclusive. It persists the reset
interval across Mac monitor restarts. Fresh valid Windows contact
immediately blocks a repeat. The dashboard and events log identify
acknowledged requests and transport failures. Dry-run shows eligibility
without sending a PiKVM command. There is no power schedule.

STATUS AND LOGS

State is in ~/Library/Application Support/SFMoMAOculus/HostMonitor.
Show-Status.command reports the status snapshot's age. events.jsonl rotates at
about 2 MiB with four backups and records changes, summaries and reset results.
PiKVM credentials and the Windows heartbeat key are excluded from those
outputs and this package. Remove-Background.command preserves state and logs.
The PiKVM module uses only the Python standard library at runtime.
