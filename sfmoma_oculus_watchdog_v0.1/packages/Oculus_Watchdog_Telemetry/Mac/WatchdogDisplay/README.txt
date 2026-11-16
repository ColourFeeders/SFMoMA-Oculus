Oculus Watchdog Display - show server power controls

Run Install-Display.command in Terminal as the same logged-in Mac user that
runs the watchdog. No sudo is needed. The display opens in the default
browser now and at subsequent logins. Run Open-Power-Display.command
to reopen it with controls. Opening http://127.0.0.1:19742/ in a new tab
directly provides status only. Use Full screen on a dedicated monitor.

The page checks the watchdog snapshot about once a second. A green Watchdog
running indicator means the last snapshot reported a running process and is
at most 10 seconds old. Last update shows the time of the latest watchdog
snapshot. When updates stop for more than 10 seconds, the display turns red
and shows the number of seconds without an update. Missing, stopped, unreadable,
and disconnected states get their own indicators.
The Windows, PiKVM and recovery cards are shown with fresh running status.

The display reads the v0.2 watchdog's existing status.json locally. The
existing watchdog starts at boot. The display opens after macOS login so it
can appear in the desktop session. The display uses the Mac's existing Python
runtime and serves the page only on 127.0.0.1 TCP port 19742.

Run Remove-Display.command to remove the display from login startup.

SHOW SERVER POWER

Use the authorized dashboard tab from the sfmoma Mac desktop. The browser
session comes from a private file for that Mac account. Anyone operating
the authorized tab can request an action after confirming it.

The Mac display offers Power on via PiKVM, Soft shutdown, Soft restart,
Hard shutdown and Hard restart. It stays at http://127.0.0.1:19742/ on the rack Mac. A confirmation
appears before each command. The Mac puts automatic recovery in maintenance
and waits for the watchdog to confirm before requesting the power action.
Maintenance stays on until an operator checks the show and explicitly clears
it. This prevents a planned shutdown from being treated like a crash.
Acknowledgement means the request was
accepted. Check the server for completion and check the physical projector
assignments after a restart.

AUTO-RECOVER

Auto-recover defaults to On with fresh settings. Existing Off and
maintenance settings survive upgrades. When both signed Windows streams
have been healthy and then go silent for 60 seconds, the watchdog uses
PiKVM to hard-reset the show server. The dashboard reports the reset
attempt and five-minute retry countdown until Windows contact returns.

The switch shows the saved recovery setting. Off puts automatic recovery
in dry-run. To resume, turn it On and confirm. This checks the host
heartbeat, Restart Guard and PiKVM ATX before ending maintenance. Manual
power controls require a live monitor. If the monitor is unavailable,
authorized AV/IT can use the native PiKVM interface.

A Windows shutdown or restart notice pauses automatic recovery until the
new startup reports a healthy host heartbeat. After five minutes the
dashboard marks the startup overdue and keeps reset paused.

If the switch is On and the card reads Paused, maintenance is holding
automatic recovery. After the show is back, switch Off then On to run
the arming checks and resume recovery. Use Open-Power-Display.command
if a status-only browser tab has the switch disabled.

Hard shutdown uses PiKVM's long press of the power switch. Hard restart
uses the motherboard reset switch. Both use the existing Mac-side PiKVM
credentials, HTTPS certificate fingerprint and installed ATX wiring. A
hard action can lose unsaved work. Use Power on via PiKVM in the dashboard
to start the server after a shutdown. The button is available when PiKVM is
reachable, ATX is idle, and the power LED reports off. It uses the same
PiKVM connection and works while Windows is off. The power-on request
follows the same confirmation and maintenance behavior.

SOFT SHUTDOWN AND RESTART SETUP

These buttons use a separate Windows OpenSSH identity named oculuspower.
The installer on the Mac can be used before Windows is available. The two
buttons become available after the SSH identity and host key are present.
The dashboard checks SSH connectivity and shutdown privilege before acting.

1. Install or update the display on the Mac mini. In Terminal as sfmoma,
   generate its dedicated SSH key. Keep the private key on the Mac.

   /usr/bin/ssh-keygen -t ed25519 -N '' -f "$HOME/Library/Application Support/SFMoMAOculus/WatchdogDisplay/windows-power"

   If rerunning setup and the key already exists, reuse it. Give IT only
   the contents of the .pub file.

2. On CP007-01, have IT use the approved Windows OpenSSH Server service
   and configure the local standard account oculuspower. Grant it the
   local "Shut down the system" right (SeShutdownPrivilege) and "Force
   shutdown from a remote system" (SeRemoteShutdownPrivilege) if its
   current policy does not already provide them. This account uses key authentication
   and does not need membership in Administrators.

   Put Windows/Invoke-OculusPower.ps1 in C:\ProgramData\OculusPower.
   Put the Mac public key in C:\ProgramData\ssh\oculuspower_authorized_keys.
   Prefix its line with from="192.0.2.135",restrict followed by a space.
   Use the Mac's actual wired IP if it is reassigned.
   Protect the script, key authorization file and their parent directories
   from edits by the account. Grant the account read access to the script
   and allow the SSH service to read the authorized key file. Existing
   site-configured ACLs and SSH settings should be reviewed by IT.

   Merge Windows/sshd-config-example.txt into the server's sshd_config,
   normally %ProgramData%\ssh\sshd_config. It restricts this account to the
   power script, public-key auth, no PTY, and no TCP or agent forwarding.
   Put it after other Match blocks, or integrate it into an existing block
   for that account. Adjust the absolute paths if Windows or ProgramData
   is elsewhere. Validate sshd configuration before restarting the service
   and confirm the allowed network path from the Mac's wired address
   192.0.2.135 to CP007-01 TCP 22 using the site's SSH and firewall rules.
   Confirm the SSH service starts automatically with Windows.

3. Obtain the server's trusted Ed25519 host public key from IT or directly
   at the Windows console. It is normally in:
     C:\ProgramData\ssh\ssh_host_ed25519_key.pub

   On the Mac create windows-known-hosts in the same folder as the
   windows-power private key. Put one line in it using the Windows IP
   followed by the key type and base64 from that Windows .pub file:
     192.0.2.136 ssh-ed25519 <Windows SSH host public key base64>

   Set both files to mode 600:
     chmod 600 "$HOME/Library/Application Support/SFMoMAOculus/WatchdogDisplay/"{windows-power,windows-known-hosts}

4. From the Mac, check the connection without requesting a power change:
     /Library/Developer/CommandLineTools/usr/bin/python3 "$HOME/Library/Application Support/SFMoMAOculus/WatchdogDisplay/display.py" --check-windows-power

   Expect "Windows power connection authenticated. Shutdown privilege is
   available." Also test that an unknown command is refused with exit
   code 10. This command does not ask for a power change:

     /usr/bin/ssh -F /dev/null -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=\"$HOME/Library/Application Support/SFMoMAOculus/WatchdogDisplay/windows-known-hosts\"" -i "$HOME/Library/Application Support/SFMoMAOculus/WatchdogDisplay/windows-power" oculuspower@192.0.2.136 invalid-command

   Verify that the configured SSH account cannot open a shell before using
   it for the show. Test a soft restart during a maintenance window. The
   script calls Windows shutdown.exe with /t 0, so Windows gives apps
   their normal shutdown handling and unsaved work can block the action.

The dashboard still starts at Mac login through its existing LaunchAgent.
The package does not change Windows service settings or network access
automatically. The hard and soft action paths need commissioning on the
actual equipment.
