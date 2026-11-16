#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$OutputDirectory = [Environment]::GetFolderPath('Desktop'),
    [string]$SyncUtilityPath
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this collector on the Windows show server.' }
if (-not $OutputDirectory) { $OutputDirectory = [IO.Path]::GetTempPath() }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$reportFolder = Join-Path $OutputDirectory ('OculusServerInfo-' + $stamp + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
$null = New-Item -ItemType Directory -Path $reportFolder -Force
$report = [ordered]@{}

function Get-NamedRegistryValues {
    param([string]$Path, [string[]]$Names)
    $values = [ordered]@{}
    if (Test-Path -LiteralPath $Path) {
        $key = Get-Item -LiteralPath $Path
        try {
            foreach ($valueName in $Names) { $values[$valueName] = $key.GetValue($valueName, $null) }
        } finally { $key.Close() }
    }
    [pscustomobject]$values
}

function Get-LaunchHints {
    param([AllowNull()][string]$Text)
    $references = @(foreach ($match in [regex]::Matches([string]$Text, '"[^"]*"|\S+')) {
        $token = $match.Value.Trim('"')
        if ($token -match '(?i)\.(exe|toe|mad|madproj|madproject|ps1|bat|cmd|vbs|lnk)$' -and
            $token -notmatch '^[\-/]' -and $token -notmatch '[=<>|?]') {
            $token
        }
    })
    $affinity = @(foreach ($match in [regex]::Matches([string]$Text, '(?i)(?:^|\s)(-gpuformonitor\s+\d+|-gpubusid\s+[a-f0-9:.]+)(?=\s|$)')) {
        $match.Groups[1].Value
    })
    [pscustomobject]@{
        ReferencedFiles = @($references | Select-Object -Unique)
        TouchDesignerAffinityFlags = $affinity
    }
}

function Invoke-CollectionSection {
    param([string]$Name, [scriptblock]$Action)
    Write-Host ('Collecting ' + $Name + '...')
    try {
        $data = @(& $Action)
        $queryIssues = @($data | Where-Object {
            $_.Status -in @('TimedOut', 'ToolNotFound', 'OutputTimedOut') -or
            ($_.Status -eq 'Completed' -and $_.ExitCode -ne 0)
        })
        $report[$Name] = [ordered]@{
            Status = $(if ($queryIssues.Count -gt 0) { 'Incomplete' } else { 'OK' })
            Data = $data
        }
    } catch {
        $report[$Name] = [ordered]@{
            Status = 'Unavailable'
            ErrorType = $_.Exception.GetType().FullName
            ErrorId = $_.FullyQualifiedErrorId
        }
        Write-Warning ($Name + ' unavailable; continuing with the remaining sections.')
    }
}

function Find-ExistingTool {
    param([string]$Name, [string[]]$Candidates)
    $found = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    return $null
}

function Invoke-BoundedQuery {
    param([string]$FilePath, [string]$Arguments, [int]$TimeoutSeconds = 15)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = $FilePath
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill() } catch { }
            return [pscustomobject]@{ Tool = $FilePath; Arguments = $Arguments; Status = 'TimedOut' }
        }
        $streamsFinished = $stdout.Wait(2000) -and $stderr.Wait(2000)
        [pscustomobject]@{
            Tool = $FilePath
            Arguments = $Arguments
            Status = $(if ($streamsFinished) { 'Completed' } else { 'OutputTimedOut' })
            ExitCode = $process.ExitCode
            Stdout = $(if ($stdout.IsCompleted) { $stdout.Result } else { '[unavailable]' })
            Stderr = $(if ($stderr.IsCompleted) { $stderr.Result } else { '[unavailable]' })
        }
    } finally { $process.Dispose() }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$report['Collector'] = [ordered]@{
    Version = '1.0'
    CollectedAt = (Get-Date).ToString('o')
    ComputerName = $env:COMPUTERNAME
    RunningAs = $identity.Name
    IsAdministrator = $isAdmin
    Is64BitProcess = [Environment]::Is64BitProcess
    PowerShellVersion = $PSVersionTable.PSVersion.ToString()
    SessionName = $env:SESSIONNAME
    SessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
    Scope = 'Read-only inventory; only report folder and ZIP are written.'
    CommandPrivacy = 'Full launch commands and script contents omitted; selected file paths and TD affinity flags only.'
    UserScope = 'HKCU and per-user startup entries belong to RunningAs. If elevation used a different account, they may differ from the exhibition user.'
}
if (-not $isAdmin) { Write-Warning 'Some paths/tasks/sync data may be unavailable without administrator rights.' }

Invoke-CollectionSection 'WindowsAndHardware' {
    $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 15
    $system = Get-CimInstance Win32_ComputerSystem -OperationTimeoutSec 15
    [pscustomobject]@{
        Windows = $os | Select-Object -Property @('Caption', 'Version', 'BuildNumber', 'OSArchitecture', 'LastBootUpTime')
        WindowsBuild = Get-NamedRegistryValues 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' @('DisplayVersion', 'CurrentBuild', 'UBR', 'EditionID', 'InstallationType')
        Computer = $system | Select-Object -Property @('Manufacturer', 'Model', 'UserName', 'PartOfDomain', 'TotalPhysicalMemory')
        Baseboard = @(Get-CimInstance Win32_BaseBoard -OperationTimeoutSec 15 | Select-Object -Property @('Manufacturer', 'Product', 'Version'))
        BIOS = @(Get-CimInstance Win32_BIOS -OperationTimeoutSec 15 | Select-Object -Property @('Manufacturer', 'SMBIOSBIOSVersion', 'ReleaseDate'))
        CPU = @(Get-CimInstance Win32_Processor -OperationTimeoutSec 15 | Select-Object -Property @('Name', 'NumberOfCores', 'NumberOfLogicalProcessors'))
        TimeZone = (Get-TimeZone).Id
    }
}

Invoke-CollectionSection 'RunningShowApplications' {
    $apps = Get-CimInstance Win32_Process -Filter "Name LIKE 'TouchDesigner%' OR Name LIKE 'TouchPlayer%' OR Name LIKE 'MadMapper%'" -OperationTimeoutSec 15
    foreach ($app in $apps) {
        $version = $null
        $versionStatus = 'PathUnavailable'
        try {
            if ($app.ExecutablePath -and (Test-Path -LiteralPath $app.ExecutablePath)) {
                $fileVersion = (Get-Item -LiteralPath $app.ExecutablePath).VersionInfo
                $version = [pscustomobject]@{ FileVersion = $fileVersion.FileVersion; ProductVersion = $fileVersion.ProductVersion }
                $versionStatus = 'OK'
            }
        } catch { $versionStatus = 'Unavailable' }
        $windowTitle = $null
        try { $windowTitle = (Get-Process -Id $app.ProcessId -ErrorAction Stop).MainWindowTitle } catch { }
        [pscustomobject]@{
            Name = $app.Name
            ProcessId = $app.ProcessId
            ParentProcessId = $app.ParentProcessId
            SessionId = $app.SessionId
            CreationDate = $app.CreationDate
            ExecutablePath = $app.ExecutablePath
            Version = $version
            VersionStatus = $versionStatus
            MainWindowTitle = $windowTitle
            LaunchHints = Get-LaunchHints $app.CommandLine
        }
    }
}

Invoke-CollectionSection 'InstalledShowApplications' {
    foreach ($root in @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    )) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($key in Get-ChildItem -LiteralPath $root) {
            try {
                $name = [string]$key.GetValue('DisplayName')
                if ($name -match '(?i)TouchDesigner|TouchPlayer|MadMapper|NVIDIA.*(Driver|Sync)') {
                    [pscustomobject]@{
                        DisplayName = $name
                        DisplayVersion = $key.GetValue('DisplayVersion')
                        InstallLocation = $key.GetValue('InstallLocation')
                        Publisher = $key.GetValue('Publisher')
                    }
                }
            } finally { $key.Close() }
        }
    }
}

Invoke-CollectionSection 'GraphicsControllers' {
    Get-CimInstance Win32_VideoController -OperationTimeoutSec 15 |
        Select-Object -Property @('Name', 'PNPDeviceID', 'DriverVersion', 'DriverDate', 'Status', 'ConfigManagerErrorCode')
}

Invoke-CollectionSection 'DesktopDisplays' {
    if (-not ('OculusInventory.DisplayReader' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace OculusInventory {
    public class DisplayInfo {
        public string DeviceName, AdapterDescription, AdapterDeviceId;
        public bool Primary, CurrentModeAvailable;
        public uint Width, Height, BitsPerPixel, RefreshHzReportedInteger, Orientation;
        public int X, Y;
    }
    public static class DisplayReader {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct DISPLAY_DEVICE {
            public int cb;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string DeviceName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceString;
            public uint StateFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceID;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceKey;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct DEVMODE {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName;
            public ushort dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
            public uint dmFields;
            public int dmPositionX, dmPositionY;
            public uint dmDisplayOrientation, dmDisplayFixedOutput;
            public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName;
            public ushort dmLogPixels;
            public uint dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
            public uint dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2;
            public uint dmPanningWidth, dmPanningHeight;
        }
        [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="EnumDisplayDevicesW")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool EnumDisplayDevices(string device, uint index, ref DISPLAY_DEVICE result, uint flags);
        [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="EnumDisplaySettingsExW")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool EnumDisplaySettingsEx(string device, int mode, ref DEVMODE result, uint flags);
        public static DisplayInfo[] Read() {
            List<DisplayInfo> result = new List<DisplayInfo>();
            for (uint i = 0; i < 128; i++) {
                DISPLAY_DEVICE d = new DISPLAY_DEVICE();
                d.cb = Marshal.SizeOf(typeof(DISPLAY_DEVICE));
                if (!EnumDisplayDevices(null, i, ref d, 0)) break;
                if ((d.StateFlags & 1) == 0 || (d.StateFlags & 8) != 0) continue;
                DEVMODE m = new DEVMODE();
                m.dmSize = (ushort)Marshal.SizeOf(typeof(DEVMODE));
                bool ok = EnumDisplaySettingsEx(d.DeviceName, -1, ref m, 0);
                result.Add(new DisplayInfo {
                    DeviceName=d.DeviceName, AdapterDescription=d.DeviceString, AdapterDeviceId=d.DeviceID,
                    Primary=(d.StateFlags & 4)!=0, CurrentModeAvailable=ok,
                    Width=m.dmPelsWidth, Height=m.dmPelsHeight, BitsPerPixel=m.dmBitsPerPel,
                    RefreshHzReportedInteger=m.dmDisplayFrequency, Orientation=m.dmDisplayOrientation,
                    X=m.dmPositionX, Y=m.dmPositionY
                });
            }
            return result.ToArray();
        }
    }
}
'@ | Out-Null
    }
    [OculusInventory.DisplayReader]::Read()
}

Invoke-CollectionSection 'NvidiaQuery' {
    $tool = Find-ExistingTool 'nvidia-smi.exe' @(
        (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'),
        (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe')
    )
    if (-not $tool) { [pscustomobject]@{ Status = 'ToolNotFound' }; return }
    Invoke-BoundedQuery $tool '--query-gpu=index,name,uuid,pci.bus_id,driver_version,memory.total --format=csv'
}

Invoke-CollectionSection 'NvidiaSyncQuery' {
    $tool = $SyncUtilityPath
    if (-not $tool) {
        $tool = Find-ExistingTool 'configureSync.exe' @(
            (Join-Path $env:ProgramFiles 'NVIDIA Corporation\Sync Utility\configureSync.exe'),
            (Join-Path $env:ProgramFiles 'NVIDIA Corporation\Sync-Utility\configureSync.exe')
        )
    }
    if (-not $tool -or -not (Test-Path -LiteralPath $tool -PathType Leaf)) {
        [pscustomobject]@{ Status = 'ToolNotFound'; Note = 'Optional query skipped. Supply -SyncUtilityPath if already installed elsewhere.' }
        return
    }
    Invoke-BoundedQuery $tool 'queryTopo'
}

Invoke-CollectionSection 'WindowsGraphicsPreferences' {
    $path = 'HKCU:\SOFTWARE\Microsoft\DirectX\UserGpuPreferences'
    if (Test-Path -LiteralPath $path) {
        $key = Get-Item -LiteralPath $path
        try {
            foreach ($valueName in $key.GetValueNames()) {
                if ($valueName -match '(?i)TouchDesigner|TouchPlayer|MadMapper') {
                    [pscustomobject]@{ Executable = $valueName; Preference = $key.GetValue($valueName) }
                }
            }
        } finally { $key.Close() }
    }
}

Invoke-CollectionSection 'AutoLogonIndicators' {
    Get-NamedRegistryValues 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' @(
        'AutoAdminLogon', 'DefaultUserName', 'DefaultDomainName', 'AutoLogonCount'
    )
}

Invoke-CollectionSection 'StartupRegistry' {
    foreach ($path in @(
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'
    )) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $key = Get-Item -LiteralPath $path
        try {
            foreach ($valueName in $key.GetValueNames()) {
                [pscustomobject]@{ Location = $path; Name = $valueName; LaunchHints = Get-LaunchHints ([string]$key.GetValue($valueName)) }
            }
        } finally { $key.Close() }
    }
}

Invoke-CollectionSection 'StartupFolders' {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in @([Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('CommonStartup'))) {
        if (-not $folder -or -not (Test-Path -LiteralPath $folder)) { continue }
        foreach ($item in Get-ChildItem -LiteralPath $folder -File) {
            $target = $null; $workingDirectory = $null; $hints = $null
            if ($item.Extension -eq '.lnk') {
                $shortcut = $shell.CreateShortcut($item.FullName)
                $target = $shortcut.TargetPath
                $workingDirectory = $shortcut.WorkingDirectory
                $hints = Get-LaunchHints $shortcut.Arguments
            }
            [pscustomobject]@{ File = $item.FullName; Target = $target; WorkingDirectory = $workingDirectory; LaunchHints = $hints }
        }
    }
}

Invoke-CollectionSection 'RelevantScheduledTasks' {
    foreach ($task in Get-ScheduledTask) {
        $actionMatch = @($task.Actions | Where-Object { ($_.Execute + ' ' + $_.Arguments) -match '(?i)TouchDesigner|TouchPlayer|MadMapper|Oculus|watchdog' }).Count -gt 0
        $showTask = ($task.TaskName + ' ' + $task.TaskPath) -match '(?i)TouchDesigner|TouchPlayer|MadMapper|Oculus|watchdog'
        if (-not ($actionMatch -or $showTask)) { continue }
        [pscustomobject]@{
            Name = $task.TaskName; Path = $task.TaskPath; State = [string]$task.State
            Principal = $task.Principal | Select-Object -Property @('UserId', 'LogonType', 'RunLevel')
            TriggerTypes = @($task.Triggers | ForEach-Object { $_.CimClass.CimClassName })
            Settings = $task.Settings | Select-Object -Property @('Enabled', 'ExecutionTimeLimit', 'MultipleInstances', 'RestartCount', 'RestartInterval', 'StartWhenAvailable')
            Actions = @($task.Actions | ForEach-Object {
                [pscustomobject]@{ Executable = $_.Execute; WorkingDirectory = $_.WorkingDirectory; LaunchHints = Get-LaunchHints $_.Arguments }
            })
        }
    }
}

Invoke-CollectionSection 'Network' {
    Get-NetIPConfiguration | ForEach-Object {
        [pscustomobject]@{
            InterfaceAlias = $_.InterfaceAlias
            InterfaceIndex = $_.InterfaceIndex
            IPv4 = @($_.IPv4Address | Select-Object -Property @('IPAddress', 'PrefixLength'))
            IPv4Gateway = @($_.IPv4DefaultGateway | Select-Object -Property @('NextHop'))
        }
    }
}

Invoke-CollectionSection 'PowerSettings' {
    $tool = Join-Path $env:SystemRoot 'System32\powercfg.exe'
    Invoke-BoundedQuery $tool '/getactivescheme'
    Invoke-BoundedQuery $tool '/query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE'
    Invoke-BoundedQuery $tool '/query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE'
}

$json = $report | ConvertTo-Json -Depth 14
$jsonPath = Join-Path $reportFolder 'OculusServerInfo.json'
$json | Set-Content -LiteralPath $jsonPath -Encoding UTF8
$summary = @(
    'SF MoMA Oculus - watchdog commissioning inventory'
    ('Collected: ' + $report['Collector'].CollectedAt)
    ('Computer: ' + $env:COMPUTERNAME)
    ('Administrator: ' + $isAdmin)
    ''
    'The JSON file contains the complete inventory. Unavailable sections are recorded.'
    'No software was installed and no show/server settings were changed.'
    'No saved passwords, full launch commands, project files, or script contents are included.'
    'The report does contain local usernames, host/IP details, application paths, and GPU IDs.'
    ''
    'SECTION STATUS'
)
foreach ($entry in $report.GetEnumerator()) {
    if ($entry.Key -eq 'Collector') { continue }
    $summary += ($entry.Key + ': ' + $entry.Value.Status + ' (' + @($entry.Value.Data).Count + ' records)')
}
$summary | Set-Content -LiteralPath (Join-Path $reportFolder 'ReadMe.txt') -Encoding UTF8

$zipPath = $reportFolder + '.zip'
try {
    Compress-Archive -Path (Join-Path $reportFolder '*') -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host ''
    Write-Host 'Diagnostic ZIP:' -ForegroundColor Green
    Write-Host $zipPath
} catch {
    Write-Warning 'ZIP creation failed. Diagnostic folder:'
    Write-Host $reportFolder
}
