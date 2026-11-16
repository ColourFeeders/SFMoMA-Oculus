#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$MacIp = '192.0.2.135',
    [ValidateRange(1024,65535)][int]$Port = 19741,
    [string]$KeyFile = '',
    [switch]$Maintenance,
    [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'

function New-HostHeartbeat {
    param([byte[]]$Key, [string]$HostName, [string]$BootId, [string]$StreamId,
          [long]$Sequence, [long]$UnixTime, [string]$Phase, [System.Collections.IDictionary]$Show)
    $payload = [ordered]@{
        schema = 'oculus.windows.host'; version = 1; host = $HostName
        boot_id = $BootId; stream_id = $StreamId; sequence = $Sequence
        sent_unix = $UnixTime; phase = $Phase
    }
    if ($null -ne $Show) { $payload['show'] = $Show }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Depth 6 -Compress))
    $hmac = [Security.Cryptography.HMACSHA256]::new($Key)
    try { $signature = ([BitConverter]::ToString($hmac.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hmac.Dispose() }
    $envelope = [ordered]@{
        payload_b64 = [Convert]::ToBase64String($bytes)
        hmac_sha256 = $signature
    }
    return ,([Text.Encoding]::UTF8.GetBytes(($envelope | ConvertTo-Json -Compress)))
}

function Read-ObserverStatus {
    param([string]$Path, [long]$SentUnix)

    try {
        $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
        try {
            if ($stream.Length -gt 131072) { return @{ state = 'unreadable' } }
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8)
            try { $snapshot = $reader.ReadToEnd() | ConvertFrom-Json }
            finally { $reader.Dispose() }
        } finally { $stream.Dispose() }

        if (-not $snapshot.generated_at_utc -or $null -eq $snapshot.applications -or $null -eq $snapshot.roles) {
            return @{ state = 'unreadable' }
        }
        $sampled = ([DateTimeOffset]$snapshot.generated_at_utc).ToUnixTimeMilliseconds()
        $age = $SentUnix * 1000 - $sampled
        if ($age -lt -1000 -or $age -ge 6000) { return @{ state = 'stale' } }
        $roles = @{}
        foreach ($role in $snapshot.roles) {
            if ($role.role -cnotin @('TD_A', 'TD_B')) { continue }
            $detail = [regex]::Replace([string]$role.detail, '[\x00-\x1f\x7f-\x9f]', ' ')
            $roles[$role.role] = @{
                state = [string]$role.state; process_match = [string]$role.process_match
                age_seconds = $role.age_seconds; fault = [string]$role.fault
                detail = $detail.Substring(0, [Math]::Min(160, $detail.Length))
            }
        }
        return @{
            state = 'reporting'; sampled_ms = $sampled; roles = $roles
            madmapper = [string]$snapshot.applications.madmapper_state
            accepted_packets = $snapshot.accepted_packets; rejected_packets = $snapshot.rejected_packets
        }
    } catch {
        if (-not [IO.File]::Exists($Path)) { return @{ state = 'missing' } }
        return @{ state = 'unreadable' }
    }
}

if ($SelfTest) {
    $testKey = [byte[]](0..31)
    $testBytes = New-HostHeartbeat $testKey 'CP007-01' 'test-boot' ('1' * 32) 1 1700000000 'running'
    $wire = [Text.Encoding]::UTF8.GetString($testBytes) | ConvertFrom-Json
    if ($wire.hmac_sha256 -ne '78d88432e4e2d5d2abce8fcbcad5fbbbcdfb461a9fa80e9923296b3766b4970d') { throw 'Host heartbeat serialization/HMAC self-test failed.' }
    $fixture = [IO.Path]::GetTempFileName()
    try {
        $sample = @{
            generated_at_utc = [DateTimeOffset]::FromUnixTimeSeconds(1700000000).ToString('o')
            applications = @{ madmapper_state = 'running' }
            roles = @(@{ role = 'TD_A'; state = 'Healthy'; process_match = 'matched'; age_seconds = 0.25 })
            accepted_packets = 747; rejected_packets = 0
        }
        [IO.File]::WriteAllText($fixture, ($sample | ConvertTo-Json -Depth 6))
        $show = Read-ObserverStatus $fixture 1700000000
        if ($show.state -ne 'reporting' -or $show.madmapper -ne 'running' -or $show.roles.TD_A.state -ne 'Healthy') {
            throw 'Fresh observer status fixture failed.'
        }
        if ((Read-ObserverStatus $fixture 1700000006).state -ne 'stale') { throw 'Stale observer status fixture failed.' }
        $testBytes = New-HostHeartbeat $testKey 'CP007-01' 'test-boot' ('1' * 32) 1 1700000000 'running' $show
        $wire = [Text.Encoding]::UTF8.GetString($testBytes) | ConvertFrom-Json
        $nested = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($wire.payload_b64)) | ConvertFrom-Json
        if ($testBytes.Length -gt 4096 -or $nested.show.roles.TD_A.process_match -ne 'matched') {
            throw 'Observer status wire fixture failed.'
        }
    } finally { [IO.File]::Delete($fixture) }
    Write-Host 'PASS: host heartbeat HMAC, observer freshness, and application status wire format.'
    return
}
if ($env:OS -ne 'Windows_NT') { throw 'Run the host beacon on Windows.' }
if ([string]::IsNullOrWhiteSpace($KeyFile)) { throw 'Supply -KeyFile with the privately transferred Mac key file.' }
$keyText = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $KeyFile).ProviderPath).Trim()
if ($keyText -notmatch '^[a-fA-F0-9]{64}$') { throw 'The shared key must contain exactly 64 hexadecimal characters.' }
$key = New-Object byte[] 32
for ($index = 0; $index -lt 32; $index++) { $key[$index] = [Convert]::ToByte($keyText.Substring($index * 2, 2), 16) }
$address = [Net.IPAddress]::Parse($MacIp)
if ($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'MacIp must be an IPv4 address.' }
$endpoint = [Net.IPEndPoint]::new($address, $Port)
$boot = (Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 15).LastBootUpTime
$bootId = $boot.ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'")
$streamId = [guid]::NewGuid().ToString('N')
$sequence = [long]0
$epoch = [DateTime]::SpecifyKind([DateTime]'1970-01-01', [DateTimeKind]::Utc)
$phase = if ($Maintenance) { 'maintenance' } else { 'running' }
$udp = [Net.Sockets.UdpClient]::new([Net.Sockets.AddressFamily]::InterNetwork)
$udp.Client.Blocking = $false
$lastWarning = [double]::NegativeInfinity
$clock = [Diagnostics.Stopwatch]::StartNew()
$observerPath = Join-Path $env:LOCALAPPDATA 'SFMoMAOculus\WatchdogObserver\observer-status.json'
Write-Host ('Sending host-progress heartbeat to ' + $MacIp + ':' + $Port + ' phase=' + $phase)
try {
    while ($true) {
        $sequence++
        $unixTime = [long][Math]::Floor(([DateTime]::UtcNow - $epoch).TotalSeconds)
        $show = Read-ObserverStatus $observerPath $unixTime
        $bytes = New-HostHeartbeat $key $env:COMPUTERNAME $bootId $streamId $sequence $unixTime $phase $show
        if ($bytes.Length -gt 4096) {
            $bytes = New-HostHeartbeat $key $env:COMPUTERNAME $bootId $streamId $sequence $unixTime $phase
        }
        try { $null = $udp.Send($bytes, $bytes.Length, $endpoint) }
        catch {
            if ($clock.Elapsed.TotalSeconds - $lastWarning -ge 10) {
                Write-Warning ('Host heartbeat send failed: ' + $_.Exception.GetType().Name)
                $lastWarning = $clock.Elapsed.TotalSeconds
            }
        }
        Start-Sleep -Seconds 1
    }
} finally { $udp.Close() }
