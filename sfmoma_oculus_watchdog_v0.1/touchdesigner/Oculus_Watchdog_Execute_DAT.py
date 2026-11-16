import json
import os
import socket
import time
import uuid

INSTANCE = "TD_A"
DESTINATION = ("127.0.0.1", 19740)
INTERVAL_SECONDS = 1.0


def get_health():
    return {
        "ready": False,
        "output_valid": False,
        "degraded": False,
        "fault": "HEALTH_CHECKS_NOT_CONFIGURED",
        "detail": "Project health checks are unconfigured.",
    }


_sock = None
_sequence = 0
_last_send = None
_last_warning = None
_stream_id = uuid.uuid4().hex


def _close_socket():
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
        _sock = None


def _warn(message):
    global _last_warning
    now = time.monotonic()
    if _last_warning is None or now - _last_warning >= 10.0:
        print("[Oculus watchdog] " + str(message)[:240])
        _last_warning = now


def onStart():
    global _sequence, _last_send, _last_warning, _stream_id
    _close_socket()
    _sequence = 0
    _last_send = None
    _last_warning = None
    _stream_id = uuid.uuid4().hex
    return


def onFrameEnd(frame):
    global _sock, _sequence, _last_send

    now = time.monotonic()
    if _last_send is not None and now - _last_send < INTERVAL_SECONDS:
        return
    _last_send = now
    _sequence += 1

    try:
        health = get_health()
        for key in ("ready", "output_valid", "degraded"):
            if type(health[key]) is not bool:
                raise TypeError(key + " must be a Python bool")
        fault = health.get("fault", "")
        detail = health.get("detail", "")
        if not isinstance(fault, str) or not isinstance(detail, str):
            raise TypeError("fault/detail must be strings")
        if health["ready"] and not health["output_valid"]:
            raise ValueError("ready=True requires output_valid=True")
        health = {key: health[key] for key in ("ready", "output_valid", "degraded")}
        health.update(fault=fault[:80], detail=detail[:240])
    except Exception as exc:
        health = dict(ready=False, output_valid=False, degraded=False,
                      fault="HEALTH_CHECK_EXCEPTION", detail=str(exc)[:240])
        _warn(health["fault"] + ": " + health["detail"])

    message = {
        "schema": "oculus.td.heartbeat",
        "version": 1,
        "instance": INSTANCE,
        "pid": os.getpid(),
        "project": os.path.normpath(os.path.join(project.folder, project.name)),
        "launch_token": os.environ.get("OCULUS_LAUNCH_TOKEN", ""),
        "stream_id": _stream_id,
        "sequence": _sequence,
        **health,
    }
    try:
        if _sock is None:
            _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _sock.setblocking(False)
        _sock.sendto(json.dumps(message, ensure_ascii=True).encode("utf-8"), DESTINATION)
    except OSError as exc:
        _close_socket()
        _warn("Heartbeat send failed: " + str(exc))
    return


def onExit():
    _close_socket()
    return
