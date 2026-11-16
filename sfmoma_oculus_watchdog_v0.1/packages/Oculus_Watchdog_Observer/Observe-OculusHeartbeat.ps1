#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath = '',
    [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
$script:utf8WithoutBom = [Text.UTF8Encoding]::new($false)

function Get-DefaultObserverConfig {
    @{
        mode = 'observe'; listen_port = 19740
        heartbeat_timeout_seconds = 5; ready_stability_seconds = 10
        startup_grace_seconds = 120; source_retention_seconds = 60
        max_sources_per_role = 16; max_packet_bytes = 4096
        max_packets_per_tick = 64; status_interval_seconds = 1
        summary_interval_seconds = 30; log_max_bytes = 2097152
        log_file_count = 5; log_directory = ''
    }
}

function Test-IntegerValue {
    param($Value)
    return ($Value -is [int] -or $Value -is [long] -or $Value -is [uint32] -or $Value -is [uint64])
}

function Read-ObserverConfig {
    param([string]$Path)
    $config = Get-DefaultObserverConfig
    $configText = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $configText.TrimStart().StartsWith('{')) { throw 'Configuration must be a JSON object.' }
    $inputConfig = ConvertFrom-Json -InputObject $configText
    if ($null -eq $inputConfig -or $inputConfig -isnot [pscustomobject]) { throw 'Configuration must be a JSON object.' }
    foreach ($property in $inputConfig.PSObject.Properties) {
        if (-not $config.ContainsKey($property.Name)) { throw ('Unknown configuration field: ' + $property.Name) }
        $config[$property.Name] = $property.Value
    }
    if ($config.mode -isnot [string] -or $config.mode -cne 'observe') { throw 'Only mode=observe is supported.' }
    $ranges = @{
        listen_port = @(1024, 65535); heartbeat_timeout_seconds = @(1, 120)
        ready_stability_seconds = @(0, 300); startup_grace_seconds = @(1, 3600)
        source_retention_seconds = @(10, 3600); max_sources_per_role = @(1, 64)
        max_packet_bytes = @(512, 16384); max_packets_per_tick = @(1, 256)
        status_interval_seconds = @(1, 30); summary_interval_seconds = @(5, 3600)
        log_max_bytes = @(65536, 16777216); log_file_count = @(1, 10)
    }
    foreach ($name in $ranges.Keys) {
        $value = $config[$name]
        if (-not (Test-IntegerValue $value) -or $value -lt $ranges[$name][0] -or $value -gt $ranges[$name][1]) {
            throw ('Invalid integer/range for configuration field: ' + $name)
        }
    }
    if ($config.source_retention_seconds -le $config.heartbeat_timeout_seconds) { throw 'source_retention_seconds must exceed heartbeat_timeout_seconds.' }
    if ($config.log_directory -isnot [string]) { throw 'log_directory must be a string.' }
    if (-not $config.log_directory) {
        if (-not $env:LOCALAPPDATA) { throw 'Set log_directory explicitly when LOCALAPPDATA is unavailable.' }
        $config.log_directory = Join-Path $env:LOCALAPPDATA 'SFMoMAOculus\WatchdogObserver'
    }
    $config.log_directory = [IO.Path]::GetFullPath($config.log_directory)
    return $config
}

function Get-RequiredField {
    param($Object, [string]$Name)
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { throw 'missing_field' }
    return ,$property.Value
}

function ConvertFrom-HeartbeatBytes {
    param([byte[]]$Bytes, [hashtable]$Config)
    if ($Bytes.Length -eq 0 -or $Bytes.Length -gt $Config.max_packet_bytes) { throw 'packet_size' }
    try {
        $text = $script:strictUtf8.GetString($Bytes)
        if (-not $text.TrimStart().StartsWith('{')) { throw 'object_required' }
        $packet = ConvertFrom-Json -InputObject $text
    } catch { throw 'invalid_json_or_utf8' }
    if ($null -eq $packet -or $packet -is [array] -or $packet -is [string]) { throw 'object_required' }
    $schema = Get-RequiredField $packet 'schema'
    if ($schema -isnot [string] -or $schema -cne 'oculus.td.heartbeat') { throw 'schema_mismatch' }
    $version = Get-RequiredField $packet 'version'
    if (-not (Test-IntegerValue $version) -or $version -ne 1) { throw 'version_mismatch' }
    $role = Get-RequiredField $packet 'instance'
    if ($role -isnot [string] -or $role -cnotin @('TD_A', 'TD_B')) { throw 'unknown_role' }
    $processId = Get-RequiredField $packet 'pid'
    $sequence = Get-RequiredField $packet 'sequence'
    if (-not (Test-IntegerValue $processId) -or $processId -le 0 -or $processId -gt [int]::MaxValue) { throw 'invalid_pid' }
    if (-not (Test-IntegerValue $sequence) -or $sequence -le 0 -or $sequence -gt [long]::MaxValue) { throw 'invalid_sequence' }
    $fields = @{}
    foreach ($name in @('project', 'stream_id', 'launch_token', 'fault', 'detail')) {
        $value = Get-RequiredField $packet $name
        if ($value -isnot [string]) { throw 'string_required' }
        $fields[$name] = $value
    }
    if (-not $fields.project -or $fields.project.Length -gt 1024) { throw 'invalid_project' }
    if ($fields.stream_id -cnotmatch '^[a-f0-9]{32}$') { throw 'invalid_stream_id' }
    if ($fields.launch_token.Length -gt 256 -or $fields.fault.Length -gt 80 -or $fields.detail.Length -gt 240) { throw 'field_too_long' }
    $flags = @{}
    foreach ($name in @('ready', 'output_valid', 'degraded')) {
        $value = Get-RequiredField $packet $name
        if ($value -isnot [bool]) { throw 'boolean_required' }
        $flags[$name] = $value
    }
    if ($flags.ready -and -not $flags.output_valid) { throw 'inconsistent_readiness' }
    $encoding = [Text.Encoding]::UTF8
    $key = [string]$processId + '|' + $fields.stream_id + '|' +
        [Convert]::ToBase64String($encoding.GetBytes($fields.project.Replace('/', '\').ToLowerInvariant())) + '|' +
        [Convert]::ToBase64String($encoding.GetBytes($fields.launch_token))
    return [pscustomobject]@{
        Role = $role; Key = $key; ProcessId = [long]$processId; Sequence = [long]$sequence
        Project = $fields.project; StreamId = $fields.stream_id; LaunchToken = $fields.launch_token
        Ready = $flags.ready; OutputValid = $flags.output_valid; Degraded = $flags.degraded
        Fault = $fields.fault; Detail = $fields.detail
    }
}

function New-ObserverState {
    return @{
        Roles = @{
            TD_A = @{ EverSeen = $false; Sources = @{} }
            TD_B = @{ EverSeen = $false; Sources = @{} }
        }
        Accepted = [long]0; Rejected = [long]0; Rejections = @{}
    }
}

function Remove-ExpiredSources {
    param([hashtable]$Role, [double]$Now, [hashtable]$Config)
    foreach ($key in @($Role.Sources.Keys)) {
        if ($Now - $Role.Sources[$key].LastSeen -ge $Config.source_retention_seconds) { $Role.Sources.Remove($key) }
    }
}

function Add-Heartbeat {
    param([hashtable]$State, $Packet, [double]$Now, [hashtable]$Config)
    $role = $State.Roles[$Packet.Role]
    Remove-ExpiredSources $role $Now $Config
    if ($role.Sources.ContainsKey($Packet.Key)) {
        $source = $role.Sources[$Packet.Key]
        if ($Packet.Sequence -le $source.Packet.Sequence) { return 'sequence_not_advancing' }
        if ($Now - $source.LastSeen -ge $Config.heartbeat_timeout_seconds) { $source.GoodSince = $null }
    } else {
        if ($role.Sources.Count -ge $Config.max_sources_per_role) { return 'source_limit' }
        $source = @{ FirstSeen = $Now; LastSeen = $Now; GoodSince = $null; HasBeenReady = $false; Packet = $Packet }
        $role.Sources[$Packet.Key] = $source
    }
    $source.LastSeen = $Now
    $source.LastReceiptUtc = [DateTime]::UtcNow.ToString('o')
    $source.Packet = $Packet
    if ($Packet.Ready -and $Packet.OutputValid -and $Packet.Fault -cne 'HEALTH_CHECKS_NOT_CONFIGURED') {
        $source.HasBeenReady = $true
        if ($null -eq $source.GoodSince) { $source.GoodSince = $Now }
    } else { $source.GoodSince = $null }
    $fresh = @($role.Sources.Values | Where-Object { $Now - $_.LastSeen -lt $Config.heartbeat_timeout_seconds })
    if ($fresh.Count -gt 1) { foreach ($other in $fresh) { $other.GoodSince = $null } }
    $role.EverSeen = $true
    $State.Accepted++
    return 'accepted'
}

function Get-ShowApplicationInventory {
    param([int]$SessionId)
    $applications = [ordered]@{
        session_id = $SessionId; inventory_state = 'ok'
        touchdesigner = @(); madmapper = @(); madmapper_state = 'unknown'
    }
    try {
        $processes = @(Get-Process -ErrorAction Stop | Where-Object {
            $_.ProcessName -in @('TouchDesigner', 'TouchPlayer', 'MadMapper')
        })
    } catch {
        $applications.inventory_state = 'unavailable'
        return [pscustomobject]$applications
    }
    foreach ($process in $processes) {
        try {
            if ($process.SessionId -ne $SessionId -or $process.HasExited) { continue }
            $entry = [ordered]@{
                pid = $process.Id; name = $process.ProcessName
                started_at_utc = $null; executable_path = $null; window_title = $null
            }
            try { $entry.started_at_utc = $process.StartTime.ToUniversalTime().ToString('o') } catch { }
            try { $entry.executable_path = $process.Path } catch { }
            try { $entry.window_title = $process.MainWindowTitle } catch { }
            if ($process.HasExited) { continue }
            if ($entry.name -eq 'MadMapper') { $applications.madmapper += [pscustomobject]$entry }
            else { $applications.touchdesigner += [pscustomobject]$entry }
        } catch {
            $applications.inventory_state = 'partial'
        } finally { $process.Dispose() }
    }
    if ($applications.madmapper.Count -gt 0) { $applications.madmapper_state = 'running' }
    elseif ($applications.inventory_state -eq 'ok') { $applications.madmapper_state = 'not_running' }
    return [pscustomobject]$applications
}

function Get-RoleStatus {
    param([string]$Name, [hashtable]$Role, [double]$Now, [hashtable]$Config, $Applications)
    Remove-ExpiredSources $Role $Now $Config
    $fresh = @($Role.Sources.Values | Where-Object { $Now - $_.LastSeen -lt $Config.heartbeat_timeout_seconds })
    $status = [ordered]@{
        role = $Name; state = 'Awaiting heartbeat'; reason = ''; source_count = $fresh.Count
        identity_verified = $false; process_match = 'no_unique_fresh_source'
        process_started_at_utc = $null; age_seconds = $null; stable_seconds = 0
        pid = $null; project = ''; stream_id = ''; ready = $false
        output_valid = $false; degraded = $false; fault = ''; detail = ''
    }
    if ($fresh.Count -gt 1) {
        $status.state = 'Duplicate role sources'; $status.reason = 'MULTIPLE_FRESH_SOURCES'
        return [pscustomobject]$status
    }
    if ($fresh.Count -eq 0) {
        if ($Role.EverSeen) { $status.state = 'Heartbeat stale'; $status.reason = 'HEARTBEAT_TIMEOUT' }
        return [pscustomobject]$status
    }
    $source = $fresh[0]; $packet = $source.Packet
    $status.age_seconds = [Math]::Round($Now - $source.LastSeen, 2)
    $status.pid = $packet.ProcessId; $status.project = $packet.Project; $status.stream_id = $packet.StreamId
    $status.ready = $packet.Ready; $status.output_valid = $packet.OutputValid
    $status.degraded = $packet.Degraded; $status.fault = $packet.Fault; $status.detail = $packet.Detail
    $status.process_match = 'inventory_unavailable'
    if ($null -ne $Applications) {
        $process = $Applications.touchdesigner | Where-Object { $_.pid -eq $packet.ProcessId } | Select-Object -First 1
        if ($null -ne $process) {
            $status.process_started_at_utc = $process.started_at_utc
            if (-not $process.started_at_utc) { $status.process_match = 'start_time_unavailable' }
            elseif ([string]::CompareOrdinal($process.started_at_utc, $source.LastReceiptUtc) -gt 0) {
                $status.process_match = 'started_after_heartbeat'
            } else { $status.process_match = 'matched' }
        } elseif ($Applications.inventory_state -eq 'ok') { $status.process_match = 'no_matching_td_process' }
    }
    if ($packet.Fault -ceq 'HEALTH_CHECKS_NOT_CONFIGURED') {
        $status.state = 'Awaiting health integration'; $status.reason = $packet.Fault
    } elseif (-not $packet.Ready -or -not $packet.OutputValid) {
        if ($packet.Fault) {
            $status.state = 'Unhealthy'; $status.reason = $packet.Fault
        } elseif ($source.HasBeenReady) {
            $status.state = 'Not ready'; $status.reason = 'OUTPUT_NOT_READY'
        } elseif ($Now - $source.FirstSeen -lt $Config.startup_grace_seconds) {
            $status.state = 'Initializing'; $status.reason = 'NOT_READY'
        } else { $status.state = 'Startup overdue'; $status.reason = 'NOT_READY' }
    } else {
        if ($null -ne $source.GoodSince) { $status.stable_seconds = [Math]::Round($Now - $source.GoodSince, 2) }
        if ($null -eq $source.GoodSince -or $Now - $source.GoodSince -lt $Config.ready_stability_seconds) {
            $status.state = 'Validating readiness'; $status.reason = 'STABILITY_WAIT'
        } elseif ($packet.Degraded) { $status.state = 'Degraded'; $status.reason = $packet.Fault }
        else { $status.state = 'Healthy' }
    }
    return [pscustomobject]$status
}

function Get-ObserverSnapshot {
    param([hashtable]$State, [double]$Now, [hashtable]$Config, $Applications)
    [pscustomobject]@{
        observer_version = '0.2.0'; mode = 'observe'; generated_at_utc = [DateTime]::UtcNow.ToString('o')
        uptime_seconds = [Math]::Round($Now, 2); identity_verification = 'Local PID correlation; sender claims unauthenticated'
        sequence_tracking_retention_seconds = $Config.source_retention_seconds
        accepted_packets = $State.Accepted; rejected_packets = $State.Rejected
        rejection_reasons = $State.Rejections
        applications = $Applications
        roles = @(foreach ($name in @('TD_A', 'TD_B')) { Get-RoleStatus $name $State.Roles[$name] $Now $Config $Applications })
    }
}

function Register-RejectedPacket {
    param([hashtable]$State, [string]$Reason)
    $State.Rejected++
    if (-not $State.Rejections.ContainsKey($Reason)) { $State.Rejections[$Reason] = [long]0 }
    $State.Rejections[$Reason]++
}

function New-ObserverLogger {
    param([hashtable]$Config)
    $null = [IO.Directory]::CreateDirectory($Config.log_directory)
    return @{
        Path = (Join-Path $Config.log_directory 'observer-events.jsonl')
        MaxBytes = [long]$Config.log_max_bytes; FileCount = [int]$Config.log_file_count
        Encoding = $script:utf8WithoutBom
    }
}

function Write-ObserverEvent {
    param([hashtable]$Logger, [string]$Event, $Data)
    $entry = [ordered]@{ at_utc = [DateTime]::UtcNow.ToString('o'); event = $Event; data = $Data }
    $line = ($entry | ConvertTo-Json -Depth 10 -Compress) + [Environment]::NewLine
    if ($Logger.Encoding.GetByteCount($line) -gt $Logger.MaxBytes) {
        $line = (@{ at_utc = [DateTime]::UtcNow.ToString('o'); event = 'entry_omitted'; original_event = $Event } | ConvertTo-Json -Compress) + [Environment]::NewLine
    }
    if ([IO.File]::Exists($Logger.Path) -and
        (Get-Item -LiteralPath $Logger.Path).Length + $Logger.Encoding.GetByteCount($line) -gt $Logger.MaxBytes) {
        $oldest = $Logger.Path + '.' + ($Logger.FileCount - 1)
        if ($Logger.FileCount -gt 1 -and [IO.File]::Exists($oldest)) { [IO.File]::Delete($oldest) }
        for ($index = $Logger.FileCount - 2; $index -ge 1; $index--) {
            $from = $Logger.Path + '.' + $index
            if ([IO.File]::Exists($from)) { [IO.File]::Move($from, ($Logger.Path + '.' + ($index + 1))) }
        }
        if ($Logger.FileCount -gt 1) { [IO.File]::Move($Logger.Path, ($Logger.Path + '.1')) }
        else { [IO.File]::Delete($Logger.Path) }
    }
    [IO.File]::AppendAllText($Logger.Path, $line, $Logger.Encoding)
}

function Write-LatestStatus {
    param([string]$Directory, $Snapshot)
    $path = Join-Path $Directory 'observer-status.json'
    $temporary = $path + '.tmp'
    [IO.File]::WriteAllText($temporary, ($Snapshot | ConvertTo-Json -Depth 10), $script:utf8WithoutBom)
    if ([IO.File]::Exists($path)) { [IO.File]::Replace($temporary, $path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $path) }
}

function Get-ConsoleText {
    param([AllowNull()][string]$Text)
    return [regex]::Replace([string]$Text, '[\x00-\x1f\x7f-\x9f]', ' ')
}

function New-LoopbackListener {
    param([int]$Port)
    $udp = [Net.Sockets.UdpClient]::new([Net.Sockets.AddressFamily]::InterNetwork)
    try {
        $udp.ExclusiveAddressUse = $true
        $udp.Client.Bind([Net.IPEndPoint]::new([Net.IPAddress]::Loopback, $Port))
        return $udp
    } catch { $udp.Close(); throw }
}

function Invoke-ObserverSelfTests {
    $config = Get-DefaultObserverConfig
    $state = New-ObserverState
    $sample = [ordered]@{
        schema = 'oculus.td.heartbeat'; version = 1; instance = 'TD_A'; pid = 100
        project = 'C:\Shows\Playback.toe'; launch_token = ''; stream_id = ('a' * 32)
        sequence = 1; ready = $false; output_valid = $false; degraded = $false
        fault = 'HEALTH_CHECKS_NOT_CONFIGURED'; detail = ''
    }
    $encoding = [Text.Encoding]::UTF8
    function Convert-TestPacket { param($Value) ConvertFrom-HeartbeatBytes ($encoding.GetBytes(($Value | ConvertTo-Json -Compress))) $config }
    function Assert-Test { param([bool]$Condition, [string]$Message) if (-not $Condition) { throw ('Self-test failed: ' + $Message) } }
    function Get-TestStatus { param([double]$Now) Get-RoleStatus 'TD_A' $state.Roles.TD_A $Now $config }
    Assert-Test ((Get-TestStatus 0).state -eq 'Awaiting heartbeat') 'never-seen role'
    $packet = Convert-TestPacket $sample
    Assert-Test ((Add-Heartbeat $state $packet 0 $config) -eq 'accepted') 'valid packet accepted'
    Assert-Test ((Get-TestStatus 0).state -eq 'Awaiting health integration') 'unconfigured adapter'
    $liveProcess = [pscustomobject]@{ pid = 100; started_at_utc = [DateTime]::UtcNow.AddMinutes(-1).ToString('o') }
    $inventory = [pscustomobject]@{ inventory_state = 'ok'; touchdesigner = @($liveProcess) }
    $matched = Get-RoleStatus 'TD_A' $state.Roles.TD_A 0 $config $inventory
    Assert-Test ($matched.process_match -eq 'matched' -and -not $matched.identity_verified) 'live PID match is not sender authentication'
    $liveProcess.started_at_utc = [DateTime]::UtcNow.AddMinutes(1).ToString('o')
    Assert-Test ((Get-RoleStatus 'TD_A' $state.Roles.TD_A 0 $config $inventory).process_match -eq 'started_after_heartbeat') 'reused PID does not match an older heartbeat'
    Assert-Test ((Add-Heartbeat $state $packet 4 $config) -eq 'sequence_not_advancing') 'duplicate sequence rejected'
    Assert-Test ((Get-TestStatus 5).state -eq 'Heartbeat stale') 'duplicates do not refresh liveness'
    $sample.sequence = 2; $sample.ready = $true; $sample.output_valid = $true; $sample.fault = ''
    $null = Add-Heartbeat $state (Convert-TestPacket $sample) 6 $config
    Assert-Test ((Get-TestStatus 6).state -eq 'Validating readiness') 'stability delay'
    foreach ($now in @(10, 14, 16)) { $sample.sequence++; $null = Add-Heartbeat $state (Convert-TestPacket $sample) $now $config }
    Assert-Test ((Get-TestStatus 16).state -eq 'Healthy') 'sustained readiness'
    $sample.sequence++; $sample.degraded = $true; $sample.fault = 'INTERACTION_UNAVAILABLE'
    $null = Add-Heartbeat $state (Convert-TestPacket $sample) 17 $config
    Assert-Test ((Get-TestStatus 17).state -eq 'Degraded') 'base playback stays useful'
    $sample.pid = 101; $sample.stream_id = ('b' * 32); $sample.sequence = 1
    $null = Add-Heartbeat $state (Convert-TestPacket $sample) 18 $config
    Assert-Test ((Get-TestStatus 18).state -eq 'Duplicate role sources') 'competing sources'
    $sample.sequence++; $null = Add-Heartbeat $state (Convert-TestPacket $sample) 23 $config
    Assert-Test ((Get-TestStatus 23).state -eq 'Validating readiness') 'unique source restabilizes'
    $sample.ready = 'false'; $rejected = $false
    try { $null = Convert-TestPacket $sample } catch { $rejected = $true }
    Assert-Test $rejected 'string boolean rejected'
    $sample.ready = $true; $sample.output_valid = $false; $rejected = $false
    try { $null = Convert-TestPacket $sample } catch { $rejected = $true }
    Assert-Test $rejected 'inconsistent readiness rejected'
    $sample.output_valid = $true
    foreach ($field in @('schema', 'version', 'ready')) {
        $original = $sample[$field]; $sample[$field] = @($original); $rejected = $false
        try { $null = Convert-TestPacket $sample } catch { $rejected = $true }
        Assert-Test $rejected ('singleton array rejected for ' + $field)
        $sample[$field] = $original
    }
    $rejected = $false
    try { $null = ConvertFrom-HeartbeatBytes ($encoding.GetBytes('[' + ($sample | ConvertTo-Json -Compress) + ']')) $config } catch { $rejected = $true }
    Assert-Test $rejected 'top-level singleton array rejected'
    $runtimeState = New-ObserverState
    $sample.degraded = $false; $sample.fault = ''; $sample.sequence++
    $null = Add-Heartbeat $runtimeState (Convert-TestPacket $sample) 0 $config
    $sample.ready = $false; $sample.output_valid = $false; $sample.sequence++
    $null = Add-Heartbeat $runtimeState (Convert-TestPacket $sample) 1 $config
    Assert-Test ((Get-RoleStatus 'TD_A' $runtimeState.Roles.TD_A 1 $config).state -eq 'Not ready') 'runtime readiness loss'
    $state = New-ObserverState
    $sample.ready = $false; $sample.output_valid = $false; $sample.degraded = $false; $sample.fault = ''; $sample.sequence++
    $null = Add-Heartbeat $state (Convert-TestPacket $sample) 24 $config
    Assert-Test ((Get-TestStatus 24).state -eq 'Initializing') 'startup grace'
    for ($now = 28; $now -le 144; $now += 4) {
        $sample.sequence++; $null = Add-Heartbeat $state (Convert-TestPacket $sample) $now $config
    }
    Assert-Test ((Get-TestStatus 144).state -eq 'Startup overdue') 'startup overdue'
    $sample.fault = 'HEALTH_CHECK_EXCEPTION'; $sample.sequence++
    $null = Add-Heartbeat $state (Convert-TestPacket $sample) 145 $config
    Assert-Test ((Get-TestStatus 145).state -eq 'Unhealthy') 'explicit fault'
    $rejected = $false
    try { $null = ConvertFrom-HeartbeatBytes ([byte[]]@(255)) $config } catch { $rejected = $true }
    Assert-Test $rejected 'invalid UTF8 rejected'
    $rejected = $false
    try { $null = ConvertFrom-HeartbeatBytes (New-Object byte[] ($config.max_packet_bytes + 1)) $config } catch { $rejected = $true }
    Assert-Test $rejected 'oversized packet rejected'
    Assert-Test ((Get-ConsoleText "bad`ntext$([char]27)") -eq 'bad text ') 'console controls removed'
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('oculus-observer-test-' + [guid]::NewGuid().ToString('N'))
    $listener = $null; $sender = $null
    try {
        $config.log_directory = $temporary; $config.log_max_bytes = 500; $config.log_file_count = 3
        $logger = New-ObserverLogger $config
        1..20 | ForEach-Object { Write-ObserverEvent $logger 'test' @{ value = ('x' * 90) } }
        $files = @(Get-ChildItem -LiteralPath $temporary -Filter 'observer-events.jsonl*')
        Assert-Test ($files.Count -le 3) 'log retention'
        foreach ($file in $files) { Assert-Test ($file.Length -le 500) 'log size limit' }
        $snapshot = Get-ObserverSnapshot $state 145 $config
        Write-LatestStatus $temporary $snapshot; Write-LatestStatus $temporary $snapshot
        Assert-Test ([IO.File]::Exists((Join-Path $temporary 'observer-status.json'))) 'latest status replacement'
        $listener = New-LoopbackListener 0
        $sender = [Net.Sockets.UdpClient]::new([Net.Sockets.AddressFamily]::InterNetwork)
        $bytes = $encoding.GetBytes(($sample | ConvertTo-Json -Compress))
        $null = $sender.Send($bytes, $bytes.Length, $listener.Client.LocalEndPoint)
        Assert-Test ($listener.Client.Poll(2000000, [Net.Sockets.SelectMode]::SelectRead)) 'UDP delivery'
        $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
        $received = $listener.Receive([ref]$remote)
        Assert-Test ((ConvertFrom-HeartbeatBytes $received $config).Fault -eq 'HEALTH_CHECK_EXCEPTION') 'real UDP payload compatibility'
    } finally {
        if ($null -ne $sender) { $sender.Close() }
        if ($null -ne $listener) { $listener.Close() }
        if ([IO.Directory]::Exists($temporary)) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
    Write-Host 'PASS: heartbeat validation, health states, PID correlation, log rotation, status replacement, and local UDP.' -ForegroundColor Green
}

if ($SelfTest) { Invoke-ObserverSelfTests; return }
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $observerScriptDirectory = $null
    if (-not [string]::IsNullOrWhiteSpace($PSCommandPath)) {
        $observerScriptDirectory = [IO.Path]::GetDirectoryName($PSCommandPath)
    }
    if ([string]::IsNullOrWhiteSpace($observerScriptDirectory)) {
        $observerInvocationPath = $MyInvocation.MyCommand.PSObject.Properties['Path']
        if ($null -ne $observerInvocationPath -and
            -not [string]::IsNullOrWhiteSpace([string]$observerInvocationPath.Value)) {
            $observerScriptDirectory = [IO.Path]::GetDirectoryName([string]$observerInvocationPath.Value)
        }
    }
    if ([string]::IsNullOrWhiteSpace($observerScriptDirectory)) {
        $observerScriptDirectory = $PSScriptRoot
    }
    if ([string]::IsNullOrWhiteSpace($observerScriptDirectory)) {
        throw 'Cannot locate the observer script folder. Run the saved .ps1 file or supply -ConfigPath.'
    }
    $ConfigPath = Join-Path -Path $observerScriptDirectory -ChildPath 'observer.config.json'
}
$config = Read-ObserverConfig $ConfigPath
$listener = $null; $logger = $null
try {
    try { $listener = New-LoopbackListener $config.listen_port }
    catch { throw ('Cannot bind 127.0.0.1:' + $config.listen_port + '. Close any existing observer or check the configured port. ' + $_.Exception.GetType().Name) }
    $logger = New-ObserverLogger $config
    $state = New-ObserverState
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $sessionId = (Get-Process -Id $PID).SessionId
    $lastStatus = [double]::NegativeInfinity; $lastSummary = [double]::NegativeInfinity
    $previous = @{}
    Write-Host ('Oculus heartbeat observer | 127.0.0.1:' + $config.listen_port)
    Write-Host ('Observation only | session ' + $sessionId)
    Write-Host ('Status and logs: ' + $config.log_directory)
    Write-ObserverEvent $logger 'observer_started' @{ mode = 'observe'; listen_port = $config.listen_port }
    while ($true) {
        if ($listener.Client.Poll(200000, [Net.Sockets.SelectMode]::SelectRead)) {
            for ($count = 0; $count -lt $config.max_packets_per_tick; $count++) {
                if (-not $listener.Client.Poll(0, [Net.Sockets.SelectMode]::SelectRead)) { break }
                $remote = [Net.IPEndPoint]::new([Net.IPAddress]::Any, 0)
                $bytes = $listener.Receive([ref]$remote)
                $now = $clock.Elapsed.TotalSeconds
                if (-not [Net.IPAddress]::IsLoopback($remote.Address)) { Register-RejectedPacket $state 'non_loopback'; continue }
                try {
                    $packet = ConvertFrom-HeartbeatBytes $bytes $config
                    $result = Add-Heartbeat $state $packet $now $config
                    if ($result -ne 'accepted') { Register-RejectedPacket $state $result }
                } catch {
                    $reason = $_.Exception.Message
                    if ($reason -notmatch '^[a-z_]{1,40}$') { $reason = 'invalid_packet' }
                    Register-RejectedPacket $state $reason
                }
            }
        }
        $now = $clock.Elapsed.TotalSeconds
        if ($now - $lastStatus -ge $config.status_interval_seconds) {
            $applications = Get-ShowApplicationInventory $sessionId
            $snapshot = Get-ObserverSnapshot $state $now $config $applications
            Write-LatestStatus $config.log_directory $snapshot
            foreach ($role in $snapshot.roles) {
                $signature = $role.state + '|' + $role.reason + '|' + $role.pid + '|' + $role.project + '|' + $role.stream_id + '|' + $role.process_match + '|' + $role.process_started_at_utc
                if (-not $previous.ContainsKey($role.role) -or $previous[$role.role] -cne $signature) {
                    $previous[$role.role] = $signature
                    Write-ObserverEvent $logger 'role_state_changed' $role
                    $line = '{0} {1}: {2}' -f (Get-Date -Format 'HH:mm:ss'), $role.role, $role.state
                    if ($role.reason) { $line += ' [' + (Get-ConsoleText $role.reason) + ']' }
                    if ($null -ne $role.pid) { $line += ' PID=' + $role.pid + ' process=' + $role.process_match }
                    Write-Host $line
                }
            }
            $madmapperProcesses = @($applications.madmapper | Sort-Object pid)
            $madmapperSignature = $applications.madmapper_state + '|' + (@($madmapperProcesses | ForEach-Object { [string]$_.pid + '/' + $_.started_at_utc }) -join ',')
            if ($previous['MadMapper'] -cne $madmapperSignature) {
                $previous['MadMapper'] = $madmapperSignature
                Write-ObserverEvent $logger 'madmapper_process_changed' @{ state = $applications.madmapper_state; processes = $madmapperProcesses }
                $line = (Get-Date -Format 'HH:mm:ss') + ' MadMapper process: ' + $applications.madmapper_state
                if ($madmapperProcesses.Count -gt 0) { $line += ' PID=' + ($madmapperProcesses.pid -join ',') }
                Write-Host $line
            }
            $lastStatus = $now
            if ($now - $lastSummary -ge $config.summary_interval_seconds) {
                Write-ObserverEvent $logger 'summary' $snapshot
                $roleSummary = @($snapshot.roles | ForEach-Object { $_.role + '=' + $_.state }) -join '; '
                Write-Host ('Summary: ' + $roleSummary + ' | accepted=' + $state.Accepted + ' rejected=' + $state.Rejected)
                $lastSummary = $now
            }
        }
    }
} finally {
    if ($null -ne $listener) { $listener.Close() }
    if ($null -ne $logger) {
        try { Write-ObserverEvent $logger 'observer_stopped' @{ mode = 'observe' } } catch { }
    }
}
