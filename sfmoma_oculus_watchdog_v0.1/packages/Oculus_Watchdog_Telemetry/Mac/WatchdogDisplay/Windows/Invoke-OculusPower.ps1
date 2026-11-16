#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

if ($env:COMPUTERNAME -ne 'CP007-01') {
    exit 12
}

if ([string]::IsNullOrEmpty($env:SSH_ORIGINAL_COMMAND)) {
    exit 10
}
switch -CaseSensitive -Exact ($env:SSH_ORIGINAL_COMMAND) {
    'status' {
        $privileges = & "$env:WINDIR\System32\whoami.exe" /priv
        if ($LASTEXITCODE -ne 0 -or -not ($privileges -match '\bSeShutdownPrivilege\b')) {
            exit 11
        }
        if (-not ($privileges -match '\bSeRemoteShutdownPrivilege\b')) {
            exit 13
        }
        Write-Output 'OCULUS_POWER_READY'
        exit 0
    }
    'shutdown' { $operation = '/s' }
    'restart' { $operation = '/r' }
    default { exit 10 }
}

& "$env:WINDIR\System32\shutdown.exe" $operation /t 0 /d p:0:0 /c 'SF MoMA Oculus operator power request'
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Write-Output 'OCULUS_POWER_ACCEPTED'
