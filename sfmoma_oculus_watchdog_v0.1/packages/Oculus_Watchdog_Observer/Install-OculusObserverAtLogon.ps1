#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ObserverDirectory = (Join-Path $env:USERPROFILE 'Downloads\Oculus_Watchdog_Observer')
)

$ErrorActionPreference = 'Stop'
$taskName = 'Oculus Application Observer'
$taskPath = '\'
if ($env:COMPUTERNAME -ne 'CP007-01') {
    throw "Run this on show server CP007-01. Current computer: $env:COMPUTERNAME."
}
if ((Get-Process -Id $PID).SessionId -eq 0) {
    throw 'Run this from the Windows desktop session that runs the show.'
}

$dir = (Resolve-Path -LiteralPath $ObserverDirectory).ProviderPath
$observer = Join-Path $dir 'Observe-OculusHeartbeat.ps1'
$config = Join-Path $dir 'observer.config.json'
foreach ($file in @($observer, $config)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Missing file: $file"
    }
}
$settingsJson = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
if ($settingsJson.mode -ne 'observe' -or $settingsJson.listen_port -ne 19740) {
    throw 'Use the Oculus observation configuration on loopback UDP 19740.'
}
if (Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue) {
    throw "Task '$taskName' already exists. Review it in Task Scheduler."
}

$engine = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
& $engine -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $observer -ConfigPath $config -SelfTest
if ($LASTEXITCODE -ne 0) {
    throw 'Observer self-test failed. No task was registered.'
}

$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$argument = '-NoProfile -NonInteractive -WindowStyle Minimized -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}"' -f $observer, $config
$action = New-ScheduledTaskAction -Execute $engine -Argument $argument -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Write-Host "Registered '$taskName' for $account at Windows sign-in."
