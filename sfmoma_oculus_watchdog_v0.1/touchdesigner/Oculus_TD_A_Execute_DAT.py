import json
import os
import socket
import time
import uuid

INSTANCE = "TD_A"
DESTINATION = ("127.0.0.1", 19740)
INTERVAL_SECONDS = 1.0
EXPECT_CONTINUOUS_SPOUT_COOKS = False
SPOUT_STALL_SECONDS = 5.0
_spout_progress = {}


def get_health():
    senders = (
        ("/SfMOMA/mapping/over_bridge/syphonspoutout1", "UPPER BRIDGE"),
        ("/SfMOMA/mapping/under_bridge/syphonspoutout1", "LowerBridge"),
    )
    expected_size = (15360, 2160)
    now = time.monotonic()
    held = []

    for path, expected_name in senders:
        node = op(path)
        if not isinstance(node, syphonspoutoutTOP):
            _spout_progress.clear()
            return dict(ready=False, output_valid=False, degraded=False,
                        fault="SPOUT_NODE_MISSING", detail=path)

        sender = passive(node)
        errors = sender.errors()
        size = (sender.width, sender.height)
        actual_name = sender.par.sendername.eval()
        if errors:
            fault, detail = "SPOUT_OPERATOR_ERROR", errors
        elif not sender.par.active.eval():
            fault, detail = "SPOUT_DISABLED", "Active is off"
        elif actual_name != expected_name:
            fault, detail = "SPOUT_NAME_MISMATCH", f"found {actual_name!r}"
        elif size != expected_size:
            fault, detail = "SPOUT_RESOLUTION_MISMATCH", f"found {size}, expected {expected_size}"
        elif (cook_count := sender.totalCooks) < 1:
            fault, detail = "SPOUT_NOT_COOKED", path
        else:
            prior_count, last_progress = _spout_progress.get(path, (None, now))
            if cook_count != prior_count:
                last_progress = now
                _spout_progress[path] = (cook_count, last_progress)
            if now - last_progress >= SPOUT_STALL_SECONDS:
                held.append(expected_name)
            continue
        _spout_progress.clear()
        return dict(ready=False, output_valid=False, degraded=False,
                    fault=fault, detail=f"{expected_name}: {detail}")

    if held:
        detail = "Spout cooks held: " + ", ".join(held)
        if EXPECT_CONTINUOUS_SPOUT_COOKS:
            return dict(ready=False, output_valid=False, degraded=False, fault="SPOUT_NOT_ADVANCING", detail=detail)
    else:
        detail = "Both TD_A Spout outputs passed local checks."
    return dict(ready=True, output_valid=True, degraded=False, fault="", detail=detail)


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
    _spout_progress.clear()
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
