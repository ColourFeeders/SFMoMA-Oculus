#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param([switch]$UpdateHandler)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$macAddress = '192.0.2.135'
$serverAddress = '192.0.2.136'
$accountName = 'oculuspower'
$marker = '# SF MoMA Oculus dashboard OpenSSH'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')

function Set-RestrictedAcl {
    param([string]$Path, [Security.Principal.SecurityIdentifier]$Reader)
    $isDirectory = [IO.Directory]::Exists($Path)
    $acl = if ($isDirectory) {
        [IO.Directory]::GetAccessControl($Path)
    } else {
        [IO.File]::GetAccessControl($Path)
    }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($ace in $acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
        $acl.RemoveAccessRuleSpecific($ace)
    }
    $acl.SetOwner($administrators)
    $inheritance = if ($isDirectory) {
        [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else { [Security.AccessControl.InheritanceFlags]::None }
    foreach ($owner in @($administrators, $system)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($owner, 'FullControl', $inheritance, 'None', 'Allow'))
    }
    if ($null -ne $Reader) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($Reader, 'ReadAndExecute', $inheritance, 'None', 'Allow'))
    }
    if ($isDirectory) { [IO.Directory]::SetAccessControl($Path, $acl) }
    else { [IO.File]::SetAccessControl($Path, $acl) }
}

if ($env:COMPUTERNAME -ne 'CP007-01') { throw 'Run on show server CP007-01.' }
if (-not (Get-NetIPAddress -IPAddress $serverAddress -ErrorAction SilentlyContinue)) {
    throw "The show server does not have its expected IP $serverAddress."
}

$sshDir = Join-Path $env:ProgramData 'ssh'
$configPath = Join-Path $sshDir 'sshd_config'
$powerDir = Join-Path $env:ProgramData 'OculusPower'
$handler = Join-Path $powerDir 'Invoke-OculusPower.ps1'
$authorizedKeys = Join-Path $powerDir 'authorized_keys'
$pending = Join-Path $sshDir 'sshd_config.oculus.pending'
$source = Join-Path $PSScriptRoot 'Invoke-OculusPower.ps1'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Extract the complete setup ZIP.' }
foreach ($path in @($sshDir, $configPath, $powerDir, $handler, $authorizedKeys, $pending)) {
    $entry = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($entry -and ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Review the existing redirected path before setup: $path"
    }
}
$managedConfig = $false
if (Test-Path -LiteralPath $configPath) {
    $managedConfig = (Get-Content -LiteralPath $configPath -TotalCount 1) -eq $marker
    if (-not $managedConfig) { throw "There is an existing SSH configuration at $configPath. Inspect it before merging." }
}
if ($UpdateHandler) {
    $handlerPath = $handler.Replace('\','/')
    $forced = (Get-Content -LiteralPath $configPath | Where-Object { $_ -match '^\s*ForceCommand\s+' })
    if (-not $managedConfig -or @($forced).Count -ne 1 -or -not $forced.EndsWith((' -File "{0}"' -f $handlerPath)) -or
        -not (Test-Path -LiteralPath $handler -PathType Leaf)) {
        throw 'The existing forced-command handler does not match this package.'
    }
    $account = Get-LocalUser -Name $accountName
    $syntaxTokens = $null; $syntaxErrors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$syntaxTokens, [ref]$syntaxErrors)
    if ($syntaxErrors.Count -gt 0) { throw 'The updated Windows power handler has syntax errors.' }
    $stagedHandler = Join-Path $powerDir ('Invoke-OculusPower-' + [guid]::NewGuid().ToString('N') + '.pending')
    $backupHandler = Join-Path $powerDir ('Invoke-OculusPower-before-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        Copy-Item -LiteralPath $source -Destination $stagedHandler
        Set-RestrictedAcl -Path $stagedHandler -Reader $account.SID
        [IO.File]::Replace($stagedHandler, $handler, $backupHandler)
    } finally {
        if (Test-Path -LiteralPath $stagedHandler) { Remove-Item -LiteralPath $stagedHandler -Force }
    }
    Write-Host "Power handler updated. Previous handler: $backupHandler"
    return
}
if (Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue) {
    throw 'TCP 22 already has a listener. Check the existing SSH session before changing its configuration.'
}
$account = Get-LocalUser -Name $accountName -ErrorAction SilentlyContinue
if ($account -and -not $managedConfig -and $account.Description -ne 'SF MoMA Oculus dashboard SSH power handler') {
    throw 'An existing oculuspower account has a different purpose.'
}
if ($account) {
    $adminMembers = Get-LocalGroupMember -SID $administrators
    if ($adminMembers | Where-Object { $_.SID -eq $account.SID }) {
        throw 'The oculuspower account is an administrator. The dashboard requires a standard account.'
    }
}

$publicKey = (Read-Host 'Paste the Mac ssh-ed25519 PUBLIC key line').Trim()
$keyMatch = [regex]::Match($publicKey, '^ssh-ed25519\s+(AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43})(?:\s+[^\r\n]*)?$')
if (-not $keyMatch.Success) { throw 'Expected the Mac public key beginning with ssh-ed25519.' }
$keyData = $keyMatch.Groups[1].Value

$capName = 'OpenSSH.Server~~~~0.0.1.0'
if ((Get-WindowsCapability -Online -Name $capName).State -ne 'Installed') {
    Write-Host 'Installing the Windows OpenSSH Server feature.'
    $result = Add-WindowsCapability -Online -Name $capName
    if ($result.RestartNeeded) { throw 'Windows reports a restart is required to finish the feature installation. Resume during a maintenance window.' }
}
$service = Get-Service -Name sshd
if ($service.Status -ne 'Stopped') { throw 'An SSH service is already active. Inspect its settings before proceeding.' }
$sshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
$keygen = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
$engine = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $keygen)) {
    $clientFeature = Add-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0'
    if ($clientFeature.RestartNeeded) { throw 'Windows reports a restart is required for the OpenSSH key tools. Resume during a maintenance window.' }
}
if ((-not (Test-Path -LiteralPath $keygen)) -or (-not (Test-Path -LiteralPath $sshd))) {
    throw 'The built-in OpenSSH executables are unavailable after installing the feature.'
}
if (Test-Path -LiteralPath $configPath) {
    if ((Get-Content -LiteralPath $configPath -TotalCount 1) -ne $marker) {
        throw "An existing SSH configuration needs review at $configPath."
    }
}
$ruleName = 'OpenSSH-Server-In-TCP'
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -Name $ruleName -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -LocalAddress $serverAddress -RemoteAddress $macAddress
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName 'Oculus OpenSSH from Mac mini' -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -LocalAddress $serverAddress -RemoteAddress $macAddress | Out-Null
}

if (-not $account) {
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    $passwordBytes = New-Object byte[] 32
    $passwordChars = New-Object char[] 44
    $password = [Security.SecureString]::new()
    try {
        $generator.GetBytes($passwordBytes)
        $length = [Convert]::ToBase64CharArray($passwordBytes, 0, $passwordBytes.Length, $passwordChars, 0)
        for ($index = 0; $index -lt $length; $index++) { $password.AppendChar($passwordChars[$index]) }
        foreach ($letter in 'Aa1!'.ToCharArray()) { $password.AppendChar($letter) }
        $password.MakeReadOnly()
        $account = New-LocalUser -Name $accountName -Password $password -PasswordNeverExpires -UserMayNotChangePassword -Description 'SF MoMA Oculus dashboard SSH power handler'
    } finally {
        $password.Dispose()
        [Array]::Clear($passwordChars, 0, $passwordChars.Length)
        [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
        $generator.Dispose()
    }
}
$usersGroup = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
if (-not (Get-LocalGroupMember -SID $usersGroup | Where-Object { $_.SID -eq $account.SID })) {
    Add-LocalGroupMember -SID $usersGroup -Member $account
}

New-Item -ItemType Directory -Path $powerDir, $sshDir -Force | Out-Null
Set-RestrictedAcl -Path $powerDir -Reader $account.SID
Copy-Item -LiteralPath $source -Destination $handler -Force
Set-RestrictedAcl -Path $handler -Reader $account.SID
[IO.File]::WriteAllText($authorizedKeys, ('from="{0}",restrict ssh-ed25519 {1}{2}' -f $macAddress, $keyData, [Environment]::NewLine), $utf8)
Set-RestrictedAcl -Path $authorizedKeys -Reader $account.SID

& $keygen -A
if ($LASTEXITCODE -ne 0) { throw 'OpenSSH host key generation failed.' }
$hostKey = Join-Path $sshDir 'ssh_host_ed25519_key'
if (-not (Test-Path -LiteralPath "$hostKey.pub" -PathType Leaf)) { throw 'The SSH host public key was not created.' }
foreach ($hostPrivateKey in Get-ChildItem -LiteralPath $sshDir -File -Filter 'ssh_host_*_key') {
    if ($hostPrivateKey.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'An existing host key is redirected. Inspect the SSH key directory.'
    }
    Set-RestrictedAcl -Path $hostPrivateKey.FullName
}

$handlerPath = $handler.Replace('\','/')
$authorizedPath = $authorizedKeys.Replace('\','/')
$hostKeyPath = $hostKey.Replace('\','/')
$configText = @"
$marker
Port 22
AddressFamily inet
ListenAddress $serverAddress
HostKey "$hostKeyPath"
AllowUsers $accountName@$macAddress
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile "$authorizedPath"
PermitTTY no
AllowTcpForwarding no
AllowAgentForwarding no
ForceCommand $engine -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$handlerPath"
"@
try {
    [IO.File]::WriteAllText($pending, ($configText + [Environment]::NewLine), $utf8)
    Set-RestrictedAcl -Path $pending
    & $sshd -t -f $pending
    if ($LASTEXITCODE -ne 0) { throw 'Windows sshd rejected the configuration.' }
    Move-Item -LiteralPath $pending -Destination $configPath -Force
} finally {
    if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force }
}

Start-Service -Name sshd
$deadline = [DateTime]::UtcNow.AddSeconds(5)
do {
    $listener = Get-NetTCPConnection -LocalAddress $serverAddress -LocalPort 22 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { Start-Sleep -Milliseconds 200 }
} while (-not $listener -and [DateTime]::UtcNow -lt $deadline)
if (-not $listener) {
    Stop-Service -Name sshd
    throw "sshd did not open $serverAddress on TCP 22."
}
Set-Service -Name sshd -StartupType Automatic

$hostPublic = (Get-Content -LiteralPath "$hostKey.pub" -TotalCount 1) -split '\s+'
Write-Host 'Windows SSH host public key:'
Write-Output ('{0} {1} {2}' -f $serverAddress, $hostPublic[0], $hostPublic[1])
