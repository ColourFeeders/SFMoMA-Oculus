#!/bin/bash
set -eu
oculus_root=$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
. "$oculus_root/mac_runtime.sh"
oculus_python=$(oculus_find_python) || exit 1
exec "$oculus_python" "$oculus_root/display.py" --install
