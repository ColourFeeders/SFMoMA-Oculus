# Installation and configuration

These instructions organize the supplied component installers. They do not assert that all components are already installed. Perform changes during an agreed maintenance window with a local operator and a recoverable backup of the existing system.

## Prepare a local deployment copy

Copy `site.example.json` to the ignored `site.local.json`. Enter the actual Mac wired IPv4 address, Windows wired IPv4 address and PiKVM IPv4 address. The three addresses must be distinct. Do not add keys or passwords to this file.

```sh
python3 tools/prepare_deployment.py --config site.local.json
```

The tool creates `deploy/` and refuses to overwrite an existing output directory. It copies the deployment files listed in the source manifest, replaces their documentation addresses, and preserves their layout and line endings. Keep the rendered copy private. When upgrading, copy the live `monitor.config.json` into the package before running the Mac installer.

Copy the full directory `deploy/packages/Oculus_Watchdog_Telemetry` to Downloads on the Mac and Windows. Copy the relevant other package directories to Windows Downloads. Use the existing show account for tasks that need its profile/session. The `CP007-01` hostname checks are deliberate safeguards, not a variable substituted by the rendering tool.

The Mac host-monitor installer specifically requires the existing **sfmoma** account. Run its launcher as that account without prefixing it with sudo; the installer requests sudo only for system installation. The Mac dashboard installer additionally requires a graphical desktop session. The packaged Mac runtime requires Python 3.9+; the repository's deployment/validation tools require Python 3.10+ and can be run on a separate preparation machine.

The config value `"mode": "observe"` is a required format label, **not a recovery-disable switch**. Recovery arming and maintenance are separate persisted state files under `~/Library/Application Support/SFMoMAOculus/HostMonitor/`. An upgrade preserves them. Explicitly select maintenance on and recovery dry-run and verify `maintenance=true` and `automatic_restart=dry_run`; do not infer safety from the config label.

## Existing installation: preferred upgrade order

1. Record the current dashboard state; enable maintenance and set recovery to dry-run. Save existing configuration, launch/service definitions and scripts privately.
2. Update Mac monitor and dashboard using `Oculus_Watchdog_Telemetry/README.txt`. Preserve the live monitor configuration. The component tests run locally; installation briefly reloads monitoring.
3. Self-test and replace only the installed Windows host sender as described in that README; stop/start only the `Oculus Host Heartbeat` task. Preserve its `.pre-telemetry` rollback copy.
4. Apply `Oculus_Observer_Low_Overhead/Update-ObserverTask.ps1` only when the base observer task exists. This compiles the console-free launcher, runs the observer self-test and switches the existing task action. It preserves the trigger, user session and config.
5. Install/update the Windows Restart Guard from the telemetry package under elevated PowerShell as the show account. It needs the existing shared heartbeat key and matching IPs. It compiles/self-tests the service and grants it only the required access to its own protected configuration.
6. Update an installed Oculus SSH configuration with `Install-OculusSSH.ps1 -UpdateHandler`. Fresh setup still refuses an existing SSH listener. Verify the local and remote shutdown rights through the existing Mac status check.
7. Verify current TD_A DAT bindings in the accepted show project. Configure TD_B separately if it is in service. Verify Windows host and guard contact, then enable Auto-recover from the power dashboard.

## Verify host recovery and telemetry

Use a planned Windows restart in dry-run to confirm the guard sends its notice and the host heartbeat resumes. In the show session, verify TD heartbeat status and MadMapper process reporting. Check Windows' bugcheck restart setting with `Get-CimInstance Win32_OSRecoveryConfiguration | Select-Object AutoReboot`. Check the dashboard for Auto-recover On and Maintenance off after commissioning. The 60-second timeout and five-minute retries apply when both signed Windows streams go silent.

Component commands assume each complete package is directly in Downloads. PowerShell command examples are intentionally one line each.

## New installation dependencies

There is no single bootstrap installer. Establish dependencies in this order:

1. Mac Python runtime: use the supplied runtime detection; component scripts expect macOS command-line Python. Install/test the Mac monitor in dry-run, generate its shared heartbeat key by its documented setup, and configure PiKVM interactively with a verified certificate fingerprint.
2. Copy the same host-heartbeat key privately into `%LOCALAPPDATA%\SFMoMAOculus\HostBeacon\host-heartbeat.key` on Windows. The first-install heartbeat installer requires this existing folder/key and the show account's password. It creates the `Oculus Host Heartbeat` boot task with a 30-second delay and does not overwrite an existing task.
3. Put `Oculus_Watchdog_Observer` in Windows Downloads and run the included `Install-OculusObserverAtLogon.ps1` from the show desktop. It creates an interactive logon task and refuses a duplicate. Keep the base observer folder at its registered path. Then apply the separate low-overhead upgrade. The base 0.2.0 script stays in place for rollback.
4. Install the Windows Restart Guard after the host heartbeat is operating. Check `restart_guard_ready` in Mac status.
5. Install the Mac dashboard and configure its restricted Windows SSH power path using the current SSH package instructions, pinning the Windows host public key verified at the Windows console.

For a fresh Mac install, from `~/Downloads/Oculus_Watchdog_Telemetry` as sfmoma, the current relative paths are:

```sh
/bin/bash ./Mac/HostMonitor/Test-Monitor.command
/bin/bash ./Mac/HostMonitor/Run-Monitor.command --maintenance on
/bin/bash ./Mac/HostMonitor/Run-Monitor.command --recovery dry-run
/bin/bash ./Mac/HostMonitor/Configure-PiKVM.command
/bin/bash ./Mac/HostMonitor/Install-Background.command
/bin/bash ./Mac/HostMonitor/Show-Status.command
/bin/bash ./Mac/WatchdogDisplay/Install-Display.command
```

`Configure-PiKVM.command` prompts for the certificate fingerprint verified at the PiKVM console and web credentials. It probes ATX status but does not reset the server. Older paths to an `Oculus_Mac_Host_Monitor_v0.2` Downloads directory in the retained component README are historical; use the current `Mac/HostMonitor/` paths above. Existing installations need the remove/reinstall sequence in the telemetry root README instead of the fresh-install command alone.

## Offline commands and venue verification

Windows PowerShell 5.1 is the intended runtime. From the correct package directory, self-tests do not install services or execute power operations:

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\Observe-OculusHeartbeat.ps1 -SelfTest
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\Send-OculusHostHeartbeat.ps1 -SelfTest
```

On the Mac, from the telemetry package:

```sh
/bin/bash ./Mac/HostMonitor/Test-Monitor.command
/bin/bash ./Mac/HostMonitor/Run-Monitor.command --maintenance on
/bin/bash ./Mac/HostMonitor/Run-Monitor.command --recovery dry-run
/bin/bash ./Mac/HostMonitor/Show-Status.command
```

Before arming, verify on the actual hardware: a normal Windows desktop restart produces boot grace and returns to observing; the boot heartbeat starts without desktop login; the application observer resumes at show-account login; no duplicate senders exist; physical projection, mapping and frame synchronization are correct; the intended PiKVM ATX reset path works in a controlled test. Arming and maintenance-off are separate states. Keep reset inhibited if boot is delayed and investigate locally.

## TouchDesigner heartbeat and inventory

Use one Execute DAT per project. Enable **Active**, **Start**, **Frame End**, and **Exit**. The DAT sends heartbeat JSON to the observer at `127.0.0.1:19740`. Use the supplied TD_A DAT for the current playback project. For TD_B, set `INSTANCE = "TD_B"` in the generic DAT and implement `get_health()` for the interactive project.

Return actual booleans for `ready`, `output_valid`, and `degraded`. Set `ready` only when `output_valid` is true. Use short `fault` and `detail` strings for diagnosis. Keep checks passive and bounded within the Frame End callback. Intended stills and the absence of visitors can be healthy states. Enable `EXPECT_CONTINUOUS_SPOUT_COOKS` in the TD_A DAT only when the approved playback state expects continuous Spout updates.

To collect an inventory, run the exporter once in the project's Python Textport. Set the project role and adjust the path to the exported source file. Add `SCOPE` to the globals to limit the export to a particular operator path.

```python
import runpy; runpy.run_path(r"C:\path\to\Export-Oculus-TD-Network.py", init_globals={"ROLE": "TD_A", "PAR_MODE": ParMode}, run_name="__main__")
```

The JSON is written beside the exporter. Review paths and parameter values before sharing it.

## Diagnostic collectors

Run the Windows collector from the show account's interactive session for desktop and per-user data. Use `-OutputDirectory` to change the report location or `-SyncUtilityPath` to point to an installed NVIDIA sync utility.

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\tools\diagnostics\Collect-OculusServerInfo.ps1
```

Run the Mac collector from Terminal on the rack Mac mini.

```sh
/bin/bash ./tools/diagnostics/Collect-OculusMacInfo.sh
```

The collectors print the resulting report path. Keep diagnostic output private and review it before sharing. Compare the inventory with the live projects, GPU assignments, projected outputs, and recovery behavior during commissioning.

## Rollback

For telemetry rollback, restore the old Windows sender first and restart its task, then restore the previous compatible Mac monitor/dashboard packages. Keep state, credentials and keys in their existing state directories. For observer rollback use the low-overhead updater's `-Restore` option; it restores the base 0.2.0 script with the console-free launcher. Do not delete the base script or config after upgrading.

Removing a background Mac service or observer does not restore the show applications. Record what was removed and retain a means to restart monitoring. Use the component `Remove-*.command` scripts only when intentionally removing their services.
