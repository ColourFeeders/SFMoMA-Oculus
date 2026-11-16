#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$Restore)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if ($env:COMPUTERNAME -ne 'CP007-01') {
    throw 'Run this update on the Oculus Windows show server, CP007-01.'
}

$taskParams = @{ TaskName = 'Oculus Application Observer'; TaskPath = '\' }
$task = Get-ScheduledTask @taskParams
$actions = @($task.Actions)
if ($actions.Count -ne 1) { throw 'The observer task must have a single existing action.' }
$current = $actions[0]
$engine = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

if ($current.Execute -eq $engine) {
    $pattern = '(?i)(?:^|\s)-File\s+"(?<script>[^"]+)"\s+-ConfigPath\s+"(?<config>[^"]+)"'
} elseif ((Split-Path -Leaf $current.Execute) -eq 'Start-OculusObserver.exe') {
    $pattern = '^"(?<script>[^"]+)"\s+"(?<config>[^"]+)"(?:\s+-Quiet)?$'
} else {
    throw 'The observer task uses an unfamiliar executable.'
}
$paths = [regex]::Match($current.Arguments, $pattern)
if (-not $paths.Success) { throw 'The observer task does not have the expected script and config paths.' }

$currentScript = $paths.Groups['script'].Value
$config = $paths.Groups['config'].Value
$directory = Split-Path -Parent $currentScript
$scriptName = Split-Path -Leaf $currentScript
if ($scriptName -notin @('Observe-OculusHeartbeat.ps1', 'Observe-OculusHeartbeat.v0.2.1.ps1')) {
    throw 'The observer task is pointing to an unfamiliar script.'
}
$launcher = Join-Path $directory 'Start-OculusObserver.exe'
$source = Join-Path $PSScriptRoot 'Observe-OculusHeartbeat.ps1'
$launcherSource = Join-Path $PSScriptRoot 'Start-Observer.cs'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'The Windows .NET Framework C# compiler is unavailable.' }
if (-not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) { throw 'Extract the full update package first.' }

if ($Restore) {
    $candidate = Join-Path $directory 'Observe-OculusHeartbeat.ps1'
    $source = $candidate
    $extra = ''
    $version = '0.2.0'
} else {
    $candidate = Join-Path $directory 'Observe-OculusHeartbeat.v0.2.1.ps1'
    $extra = ' -Quiet'
    $version = '0.2.1'
}
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "The observer script is missing: $source" }

$logDirectory = (Join-Path $env:LOCALAPPDATA 'SFMoMAOculus\WatchdogObserver')
$launchError = Join-Path $logDirectory 'observer-launch-error.txt'
$configData = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
$logProperty = $configData.PSObject.Properties['log_directory']
if ($null -ne $logProperty -and $logProperty.Value) {
    $logDirectory = [IO.Path]::GetFullPath(
        [IO.Path]::Combine($directory, [string]$logProperty.Value))
}
$statusPath = Join-Path $logDirectory 'observer-status.json'

$stage = (New-Item -ItemType Directory -Path (Join-Path $env:TEMP ('OculusObserver-' + [guid]::NewGuid().ToString('N')))).FullName
try {
    $stagedLauncher = Join-Path $stage 'Start-OculusObserver.exe'
    & $compiler /nologo /target:winexe /platform:x64 "/out:$stagedLauncher" $launcherSource
    if ($LASTEXITCODE -ne 0) { throw 'The console-free observer launcher did not compile.' }

    $selfTestArgs = '"{0}" "{1}" -SelfTest' -f $source, $config
    $selfTest = Start-Process -FilePath $stagedLauncher -ArgumentList $selfTestArgs -Wait -PassThru
    if ($selfTest.ExitCode -ne 0) { throw "Observer self-test failed. See $launchError" }
    Write-Host 'PASS: observer self-test through the console-free launcher.'

    $task = Get-ScheduledTask @taskParams
    if ($task.State -eq 'Running') {
        Stop-ScheduledTask @taskParams
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 200
            $task = Get-ScheduledTask @taskParams
        } while ($task.State -eq 'Running' -and [DateTime]::UtcNow -lt $deadline)
        if ($task.State -eq 'Running') { throw 'The existing observer task did not stop.' }
    }

    Copy-Item -LiteralPath $stagedLauncher -Destination $launcher -Force
    if ($source -ne $candidate) { Copy-Item -LiteralPath $source -Destination $candidate -Force }
    $arguments = '"{0}" "{1}"{2}' -f $candidate, $config, $extra
    $newAction = New-ScheduledTaskAction -Execute $launcher -Argument $arguments -WorkingDirectory $directory
    Set-ScheduledTask @taskParams -Action $newAction | Out-Null

    $startAt = [DateTime]::UtcNow
    Start-ScheduledTask @taskParams
    $deadline = $startAt.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 300
        $statusFile = Get-Item -LiteralPath $statusPath -ErrorAction SilentlyContinue
        if ($statusFile -and $statusFile.LastWriteTimeUtc -ge $startAt) {
            try { $snapshot = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json }
            catch { continue }
            if ($snapshot.observer_version -eq $version) {
                Write-Host "Oculus observer v$version is reporting. Manual starts and sign-in use the console-free launcher."
                return
            }
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    $result = (Get-ScheduledTaskInfo @taskParams).LastTaskResult
    throw "The console-free action is installed, but no fresh v$version status was seen. Task result: $result. Check $launchError and $statusPath"
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
