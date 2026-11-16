#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$count = 0
Get-ChildItem -LiteralPath $root -Recurse -Filter *.ps1 | Where-Object { $_.FullName -notmatch '[\\/]deploy[\\/]' } | ForEach-Object {
    $tokens = $null; $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) { throw ($errors | Out-String) }
    $count++
}
$engine = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$scripts = @('packages\Oculus_Watchdog_Telemetry\Windows\Send-OculusHostHeartbeat.ps1', 'packages\Oculus_Watchdog_Observer\Observe-OculusHeartbeat.ps1', 'packages\Oculus_Observer_Low_Overhead\Observe-OculusHeartbeat.ps1')
foreach ($script in $scripts) {
    & $engine -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $root $script) -SelfTest
    if ($LASTEXITCODE -ne 0) { throw "Self-test failed: $script" }
}
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$stage = Join-Path $env:TEMP ('OculusSourceTest-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    $guard = Join-Path $stage 'OculusRestartGuard.exe'
    & $csc /nologo /target:winexe /platform:x64 "/out:$guard" (Join-Path $root 'packages\Oculus_Watchdog_Telemetry\Windows\OculusRestartGuard.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Restart guard compile failed.' }
    $test = Start-Process -FilePath $guard -ArgumentList '--self-test' -Wait -PassThru
    if ($test.ExitCode -ne 0) { throw 'Restart guard self-test failed.' }
    $launcher = Join-Path $stage 'Start-OculusObserver.exe'
    & $csc /nologo /target:winexe /platform:x64 "/out:$launcher" (Join-Path $root 'packages\Oculus_Observer_Low_Overhead\Start-Observer.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Observer launcher compile failed.' }
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
Write-Host "PASS: parsed $count PowerShell sources; sender/observer self-tests; C# compilation and guard signing self-test. No services installed or power actions invoked."
