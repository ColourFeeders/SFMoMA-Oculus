#!/bin/bash
set -eu
if [ "$(/usr/bin/uname -s)" != 'Darwin' ]; then
    printf '%s\n' 'Run this launcher on the rack Mac mini.' >&2
    exit 1
fi
oculus_root=$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
. "$oculus_root/mac_runtime.sh"
oculus_python=$(oculus_find_python) || exit 1
printf 'Using Python: %s\n' "$oculus_python"
exec "$oculus_python" "$oculus_root/mac_host_monitor.py" --config "$oculus_root/monitor.config.json" "$@"
