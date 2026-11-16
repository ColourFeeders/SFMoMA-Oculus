#!/bin/bash
set -u
set -o pipefail
umask 077

if [ "$(/usr/bin/uname -s)" != "Darwin" ]; then
    printf '%s\n' 'Run this collector in Terminal on the rack Mac mini.' >&2
    exit 1
fi

oculus_base="${HOME}/Desktop"
if [ ! -d "$oculus_base" ] || [ ! -w "$oculus_base" ]; then
    oculus_base="${TMPDIR:-/tmp}"
fi
oculus_stamp=$(/bin/date '+%Y%m%d-%H%M%S') || exit 1
oculus_folder=$(/usr/bin/mktemp -d "${oculus_base%/}/OculusMacInfo-${oculus_stamp}-XXXXXX") || exit 1
oculus_report="${oculus_folder}/OculusMacInfo.txt"
oculus_zip="${oculus_folder}.zip"

oculus_section() {
    printf '\n=== %s ===\n' "$1"
    shift
    if command -v "$1" >/dev/null 2>&1; then
        "$@" 2>&1
        oculus_result=$?
        if [ "$oculus_result" -ne 0 ]; then
            printf '[Unavailable or incomplete; command exit status %s]\n' "$oculus_result"
        fi
    else
        printf '[Built-in command not found]\n'
    fi
    return 0
}

oculus_ipv4_interfaces() {
    /sbin/ifconfig -a | /usr/bin/awk '
        /^[[:alnum:]_]+:/ { device=$1 }
        /^[[:space:]]+inet / { print device, $0 }
        /^[[:space:]]+status:/ { print device, $0 }
    '
}

oculus_runtime_paths() {
    printf '%s\n' 'Paths only: runtimes are NOT executed or installed.'
    for oculus_name in python3 node; do
        oculus_path=$(command -v "$oculus_name" 2>/dev/null) || oculus_path=''
        if [ -n "$oculus_path" ]; then
            printf '%s from current PATH: %s\n' "$oculus_name" "$oculus_path"
        else
            printf '%s from current PATH: not found\n' "$oculus_name"
        fi
    done
    for oculus_path in \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 \
        /opt/homebrew/bin/node /usr/local/bin/node \
        /bin/bash /bin/zsh /usr/bin/curl /usr/bin/ssh /bin/launchctl; do
        if [ -x "$oculus_path" ]; then
            /bin/ls -ld "$oculus_path"
        fi
    done
    printf '%s\n' 'A path alone does not confirm a usable runtime; /usr/bin/python3 may be a developer-tools stub.'
}

{
    printf '%s\n' 'SF MoMA Oculus - Mac watchdog preflight v1.0'
    printf 'Collected: %s\n' "$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')"
    printf '%s\n' \
        'Read-only inventory. Only this report folder and ZIP are created.' \
        'No passwords, keys, environment dumps, installed-job contents, or serial numbers are collected.' \
        'This report includes local account/host names, IP addresses, and executable paths.'

    oculus_section 'macOS version/build' /usr/bin/sw_vers
    oculus_section 'Current process architecture' /usr/bin/uname -m
    oculus_section 'Mac hardware model' /usr/sbin/sysctl -n hw.model
    oculus_section 'ARM64 hardware capability (1 = supported)' /usr/sbin/sysctl -n hw.optional.arm64
    oculus_section 'Memory in bytes' /usr/sbin/sysctl -n hw.memsize
    oculus_section 'Computer name' /usr/sbin/scutil --get ComputerName
    oculus_section 'Local hostname' /usr/sbin/scutil --get LocalHostName
    oculus_section 'User running collector' /usr/bin/id -un
    oculus_section 'Console user (loginwindow means no logged-in console user)' /usr/bin/stat -f '%Su' /dev/console
    oculus_section 'Uptime' /usr/bin/uptime
    oculus_section 'IPv4/interface summary' oculus_ipv4_interfaces
    oculus_section 'Default IPv4 route (local routing table only)' /sbin/route -n get default
    oculus_section 'Route to last recorded Windows IP 192.0.2.136 (no packets sent)' /sbin/route -n get 192.0.2.136
    oculus_section 'Power/sleep configuration' /usr/bin/pmset -g custom
    oculus_section 'Configured automatic-login account, if readable' /usr/bin/defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser
    oculus_section 'FileVault status, if readable' /usr/bin/fdesetup status
    oculus_section 'Developer-tools directory, if already configured' /usr/bin/xcode-select -p
    oculus_section 'Existing executable locations' oculus_runtime_paths
} > "$oculus_report" 2>&1

if [ ! -s "$oculus_report" ]; then
    printf '%s\n' 'Could not create the report.' >&2
    exit 1
fi

if [ -x /usr/bin/zip ] && /usr/bin/zip -j -q "$oculus_zip" "$oculus_report"; then
    printf '\nDiagnostic ZIP:\n%s\n' "$oculus_zip"
else
    printf '\nDiagnostic report:\n%s\n' "$oculus_report"
fi
