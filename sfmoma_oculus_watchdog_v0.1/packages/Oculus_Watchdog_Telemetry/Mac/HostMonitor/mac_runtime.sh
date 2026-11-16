#!/bin/bash
oculus_find_python() {
    if [ -n "${OCULUS_PYTHON:-}" ]; then
        if [ ! -x "$OCULUS_PYTHON" ]; then
            printf '%s\n' 'OCULUS_PYTHON is not an executable file.' >&2
            return 1
        fi
        if "$OCULUS_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
            printf '%s\n' "$OCULUS_PYTHON"
            return 0
        fi
        printf '%s\n' 'OCULUS_PYTHON must be Python 3.9 or newer.' >&2
        return 1
    fi
    for oculus_candidate in \
        /Library/Developer/CommandLineTools/usr/bin/python3 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if [ -x "$oculus_candidate" ] && \
            "$oculus_candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
            printf '%s\n' "$oculus_candidate"
            return 0
        fi
    done
    printf '%s\n' \
        'No usable existing Python 3.9+ interpreter found.' \
        'Nothing was installed. Supply its path as OCULUS_PYTHON, or send this message back.' >&2
    return 1
}
