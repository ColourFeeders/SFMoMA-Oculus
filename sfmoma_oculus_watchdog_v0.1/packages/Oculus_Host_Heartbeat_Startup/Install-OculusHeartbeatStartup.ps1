#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$BeaconDirectory = (Join-Path $env:LOCALAPPDATA 'SFMoMAOculus\HostBeacon')
)

$ErrorActionPreference = 'Stop'
$taskName = 'Oculus Host Heartbeat'
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($env:COMPUTERNAME -ne 'CP007-01') {
    throw "Run this on the Oculus show server CP007-01. This computer is $env:COMPUTERNAME."
}

$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$taskFolder = $scheduler.GetFolder('\')
foreach ($task in $taskFolder.GetTasks(1)) {
    if ($task.Name -eq $taskName) {
        throw "The '$taskName' task already exists. Inspect it in Task Scheduler. No task was changed."
    }
}

$directory = (Resolve-Path -LiteralPath $BeaconDirectory).ProviderPath
$sender = Join-Path $directory 'Send-OculusHostHeartbeat.ps1'
$keyFile = Join-Path $directory 'host-heartbeat.key'
if (-not (Test-Path -LiteralPath $keyFile -PathType Leaf)) {
    throw "The existing Mac heartbeat key is missing from $directory. Copy the same key privately before installing."
}
if ([IO.File]::ReadAllText($keyFile).Trim() -notmatch '^[0-9a-fA-F]{64}$') {
    throw 'The heartbeat key must contain exactly 64 hex characters.'
}
if (-not (Test-Path -LiteralPath $sender -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Send-OculusHostHeartbeat.ps1') -Destination $sender
}

$engine = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
& $engine -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $sender -SelfTest
if ($LASTEXITCODE -ne 0) {
    throw "The heartbeat sender self-test failed (exit code $LASTEXITCODE). No task was registered."
}

$passwordLogon = 1
$leastPrivilege = 0
$bootTrigger = 8
$ignoreNewInstances = 2
$executeAction = 0
$createOnly = 2

$definition = $scheduler.NewTask(0)
$definition.RegistrationInfo.Description = 'Sends the signed Windows host heartbeat to the Oculus Mac watchdog.'
$definition.Principal.UserId = $account
$definition.Principal.LogonType = $passwordLogon
$definition.Principal.RunLevel = $leastPrivilege

$trigger = $definition.Triggers.Create($bootTrigger)
$trigger.Delay = 'PT30S'
$trigger.Enabled = $true

$settings = $definition.Settings
$settings.ExecutionTimeLimit = 'PT0S'
$settings.MultipleInstances = $ignoreNewInstances
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
$settings.RestartInterval = 'PT1M'
$settings.RestartCount = 3

$action = $definition.Actions.Create($executeAction)
$action.Path = $engine
$action.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -KeyFile "{1}"' -f $sender, $keyFile
$action.WorkingDirectory = $directory

$password = Read-Host -AsSecureString "Windows account password for $account (not a PIN)"
if ($password.Length -eq 0) {
    $password.Dispose()
    throw 'A Windows account password is required to register the startup task.'
}
$passwordBuffer = [IntPtr]::Zero
try {
    $passwordBuffer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
    $null = $taskFolder.RegisterTaskDefinition($taskName, $definition, $createOnly, $account,
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordBuffer), $passwordLogon, $null)
} finally {
    if ($passwordBuffer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordBuffer)
    }
    $password.Dispose()
}

Write-Host "Installed '$taskName' for $account. It starts 30 seconds after Windows boots."
