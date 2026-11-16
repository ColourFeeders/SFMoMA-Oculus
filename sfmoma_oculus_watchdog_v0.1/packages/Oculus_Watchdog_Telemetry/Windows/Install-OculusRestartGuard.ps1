#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$KeyFile = (Join-Path $env:LOCALAPPDATA 'SFMoMAOculus\HostBeacon\host-heartbeat.key'),
    [string]$MacIp = '192.0.2.135',
    [string]$WindowsIp = '192.0.2.136',
    [ValidateRange(1024,65535)][int]$Port = 19741
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

if ($env:COMPUTERNAME -ne 'CP007-01') { throw 'Run on the Oculus show server CP007-01.' }
foreach ($address in @($MacIp, $WindowsIp)) {
    if ([Net.IPAddress]::Parse($address).AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'Use an IPv4 address for the Mac and show server.'
    }
}
if ($MacIp -eq $WindowsIp) { throw 'The Mac and Windows IP addresses must be different.' }
if (-not (Get-NetIPAddress -IPAddress $WindowsIp -ErrorAction SilentlyContinue)) {
    throw "This machine does not own $WindowsIp."
}
$key = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $KeyFile).ProviderPath).Trim()
if ($key -notmatch '\A[a-fA-F0-9]{64}\z') { throw 'The installed heartbeat key is invalid.' }

$name = 'OculusRestartGuard'
$account = 'NT AUTHORITY\LocalService'
$directory = Join-Path $env:ProgramData $name
$program = Join-Path $directory 'OculusRestartGuard.exe'
$config = Join-Path $directory 'guard.config'
$image = '"' + $program + '"'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'OculusRestartGuard.cs'
$sc = Join-Path $env:WINDIR 'System32\sc.exe'

foreach ($path in @($directory, $program, $config)) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "The guard installation path is redirected: $path"
    }
}
$existing = Get-CimInstance Win32_Service -Filter "Name='$name'"
if ($existing -and ($existing.PathName -ne $image -or $existing.StartName -ne $account)) {
    throw 'The existing service configuration is unfamiliar.'
}

$stage = (New-Item -ItemType Directory -Path (Join-Path $env:TEMP ('OculusRestartGuard-' + [guid]::NewGuid().ToString('N')))).FullName
$created = $false
try {
    $stagedProgram = Join-Path $stage 'OculusRestartGuard.exe'
    & $csc /nologo /target:winexe /platform:x64 "/out:$stagedProgram" $source
    if ($LASTEXITCODE -ne 0) { throw 'Restart Guard did not compile.' }
    $test = Start-Process -FilePath $stagedProgram -ArgumentList '--self-test' -Wait -PassThru
    if ($test.ExitCode -ne 0) { throw 'Restart Guard HMAC self-test failed.' }

    if (-not $existing) {
        $credential = [pscredential]::new($account, [Security.SecureString]::new())
        New-Service -Name $name -BinaryPathName $image -DisplayName 'Oculus Windows Restart Guard' -StartupType Automatic -Credential $credential | Out-Null
        $created = $true
    } else {
        Stop-Service -Name $name
    }
    & $sc sidtype $name unrestricted | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure the Restart Guard service identity.' }

    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $serviceIdentity = [Security.Principal.NTAccount]::new('NT SERVICE', $name)
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($admins)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($principal in @($admins, $system, $serviceIdentity)) {
        $rights = if ($principal -eq $serviceIdentity) { 'ReadAndExecute' } else { 'FullControl' }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $principal, $rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    [IO.Directory]::SetAccessControl($directory, $acl)
    Copy-Item -LiteralPath $stagedProgram -Destination $program -Force
    [IO.File]::WriteAllLines($config, [string[]]@($MacIp, $WindowsIp, [string]$Port, $key), [Text.Encoding]::ASCII)
    & $sc failure $name 'reset=' '86400' 'actions=' 'restart/5000/restart/15000' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure service restart on failure.' }
    & $sc failureflag $name 1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure service recovery for startup errors.' }
    Start-Service -Name $name
    (Get-Service -Name $name).WaitForStatus('Running', [TimeSpan]::FromSeconds(8))
} catch {
    if ($created) {
        & $sc delete $name | Out-Null
    }
    throw
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host 'PASS: Restart Guard compiled and installed.'
