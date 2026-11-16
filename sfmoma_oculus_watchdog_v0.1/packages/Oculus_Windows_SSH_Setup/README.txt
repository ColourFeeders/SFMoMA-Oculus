SF MoMA Oculus dashboard SSH setup

For an installed Oculus SSH configuration, use the updated handler only:

   powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Oculus_Windows_SSH_Setup\Install-OculusSSH.ps1" -UpdateHandler

Run elevated on CP007-01. It verifies the installed forced command, saves
the prior handler, and replaces the script. Then rerun the Mac's read-only
SSH check. No power action is sent by the update or status check.

This package configures the Windows show server CP007-01 at 192.0.2.136
for the Mac mini at 192.0.2.135. It targets a fresh Windows OpenSSH setup.
The existing dashboard uses the account oculuspower and invokes the
bundled handler with status, shutdown, or restart. The installer validates
the config with Windows sshd before starting its service.

1. Copy the Mac public key

On the Mac mini, as sfmoma, print the public key for the dashboard:

   /usr/bin/ssh-keygen -y -f "$HOME/Library/Application Support/SFMoMAOculus/WatchdogDisplay/windows-power"

Copy the line beginning ssh-ed25519. Transport only that public line.

2. Install on the Windows show server

Extract Oculus_Windows_SSH_Setup into Downloads on CP007-01. Paste this
single line into PowerShell. Approve UAC if prompted.

   Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Verb RunAs -ArgumentList ('-NoProfile -NoExit -ExecutionPolicy Bypass -File "{0}"' -f "$env:USERPROFILE\Downloads\Oculus_Windows_SSH_Setup\Install-OculusSSH.ps1")

Paste the Mac public key when prompted. The installer uses Windows servicing
to install OpenSSH Server and adds the client key tools if ssh-keygen is
missing. The Windows firewall rule permits TCP 22 from the Mac IP. OpenSSH
listens on the show server's IP and accepts only oculuspower at the Mac IP.
The account is a standard local user. Its authorized key and handler are
protected from modification by that account. If the feature install reports
a required restart, resume during a maintenance window.

Copy the final Windows host public key line, starting 192.0.2.136
ssh-ed25519, directly from the Windows console.

3. Pin the Windows host key and check status

On the Mac, paste the copied line between the single quotes in place of
PASTE_WINDOWS_HOST_KEY_LINE. This is the trusted known-hosts entry.

   d="$HOME/Library/Application Support/SFMoMAOculus/WatchdogDisplay"; (umask 077; printf '%s\n' 'PASTE_WINDOWS_HOST_KEY_LINE' > "$d/windows-known-hosts") && chmod 600 "$d/windows-power" "$d/windows-known-hosts" && /Library/Developer/CommandLineTools/usr/bin/python3 "$d/display.py" --check-windows-power

Expected: Windows power connection authenticated. Shutdown privilege is
available. The check sends the read-only status request.

If SSH authenticates but status returns exit code 11, the dedicated
account lacks SeShutdownPrivilege. Under Local Security Policy, User Rights
Assignment, add CP007-01\oculuspower to Shut down the system, preserving the
other principals. The next SSH session will pick up the effective policy.
If venue Group Policy controls that setting, have the managed policy updated.
Keep the account a standard user.
Exit code 13 means the same account needs Force shutdown from a remote
system. Preserve the other principals assigned to that right.

Use the dashboard power controls for the eventual soft restart during an
available maintenance window. They put the watchdog in maintenance mode
before issuing a power request.

Local validation exercised Ed25519 input validation and OpenSSH config
parsing with a generated fixture on Linux. Verify Windows service startup
and the read-only dashboard status on the show hardware.
