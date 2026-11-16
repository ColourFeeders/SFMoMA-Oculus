from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re

SYSTEM_ROOTS = {"/sys", "/ui"}
SECRET_NAME = re.compile(
    r"passw(or)?d|passwd|secret|token|api.?key|private.?key|credential|authorization",
    re.IGNORECASE,
)
ROUTING_PARAMETERS = {
    "active", "address", "allowcooking", "autoplay", "bind", "callbackdat", "camera", "chop",
    "callbacks", "clone", "codec", "colormap", "connect", "cookalways", "dat", "device",
    "deviceid", "enable", "enableexttox", "executeloc", "externaltox", "file",
    "format", "frameend", "framestart", "fromop", "geometry", "height", "host", "index",
    "input", "ip", "listen", "localaddress", "localport", "material", "movie", "networkport",
    "op", "operator", "outputresolution", "play", "port", "protocol", "resolutionh",
    "resolutionw", "resx", "resy", "receivername", "remoteaddress", "remoteport", "renderinput",
    "sendername", "sensor", "server", "start", "stream", "streamname", "switch",
    "target", "top", "url", "usespoutactivesender", "width", "winop",
}
ENDPOINTS = {
    "touchinTOP", "touchoutTOP", "syphonspoutinTOP", "syphonspoutoutTOP",
    "ndiinTOP", "ndioutTOP", "videostreaminTOP", "videostreamoutTOP",
    "videodeviceinTOP", "videodeviceoutTOP", "moviefileinTOP",
    "oakdeviceCHOP", "oakselectCHOP", "oakselectTOP", "oakselectPOP",
    "windowCOMP", "switchTOP", "udpinDAT", "udpoutDAT", "oscinDAT", "oscoutDAT",
    "oscinCHOP", "oscoutCHOP", "tcpipDAT", "webserverDAT",
}


def read(item, name, default=None):
    try:
        return getattr(item, name)
    except Exception:
        return default


def shorten(value, limit):
    if not isinstance(value, str):
        return None
    if "://" in value:
        return "[URL omitted]"
    return value if len(value) <= limit else value[:limit] + " [truncated]"


def stored_value(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return shorten(value, 1024)
    if isinstance(value, (tuple, list)):
        return [stored_value(item) for item in value[:64]]
    path = read(value, "path")
    return path if isinstance(path, str) else None


MODE_NAMES = ("constant", "expression", "export", "bind")


def parameter_mode(mode, enum):
    for name in MODE_NAMES:
        member = read(enum, name.upper())
        if member is not None and mode == member:
            return name
    name = read(mode, "name")
    if not isinstance(name, str):
        name = str(mode).rsplit(".", 1)[-1]
    name = name.lower()
    if name in MODE_NAMES:
        return name
    raise ValueError(f"Unrecognized parameter mode: {name!r}")


def parameter_settings(node, enum, gaps):
    settings = {}
    for par in node.pars():
        try:
            if read(par, "isPulse", False) or read(par, "password", False):
                continue
            name = par.name
            if SECRET_NAME.search(name) or SECRET_NAME.search(par.label):
                continue
            mode = parameter_mode(par.mode, enum)
            entry = {"label": par.label}
            if mode == "expression":
                entry.update(mode="expression", expression=shorten(par.expr, 1024))
            elif mode == "bind":
                entry.update(mode="bind", expression=shorten(par.bindExpr, 1024))
            elif mode == "export":
                entry.update(mode="export", source_operator=read(read(par, "exportOP"), "path"))
            elif mode == "constant":
                if not (read(par, "isOP", False) or name.lower() in ROUTING_PARAMETERS):
                    continue
                value = stored_value(par.val)
                if value is None or value == "" or value == []:
                    continue
                entry.update(mode="constant", value=value)
            settings[name] = entry
        except Exception as exc:
            gaps.append({"path": node.path, "field": "parameter",
                         "name": read(par, "name"), "reason": type(exc).__name__})
    return settings


def connection_data(node):
    inputs = []
    for attribute, kind in (("inputConnectors", "operator"), ("inputCOMPConnectors", "component")):
        for connector in read(node, attribute, ()):
            for upstream in connector.connections:
                inputs.append({
                    "kind": kind,
                    "from": upstream.owner.path,
                    "output_index": upstream.index,
                    "input_index": connector.index,
                })
    ports = []
    if node.family == "COMP":
        for attribute, direction, member in (
            ("inputConnectors", "input", "inOP"),
            ("outputConnectors", "output", "outOP"),
        ):
            for connector in read(node, attribute, ()):
                internal = read(connector, member)
                if internal is not None:
                    ports.append({
                        "direction": direction,
                        "index": connector.index,
                        "internal_operator": internal.path,
                    })
    return inputs, ports


def inventory(td, role, scope="/", parameter_modes=None):
    root = td.op(scope)
    if root is None or root.family != "COMP":
        raise ValueError(f"No component at {scope!r}")
    enum = parameter_modes if parameter_modes is not None else read(td, "ParMode")
    operators = []
    gaps = []
    todo = [(root, None)]
    while todo:
        node, parent = todo.pop()
        path = node.path
        if path in SYSTEM_ROOTS:
            continue
        row = {"path": path, "parent": parent, "family": node.family, "type": node.opType}
        operators.append(row)
        row["position"] = {"x": read(node, "nodeX"), "y": read(node, "nodeY")}
        row["flags"] = {
            key: value for key in ("bypass", "lock", "display", "render", "allowCooking")
            if isinstance(value := read(node, key), bool)
        }
        row["cook_snapshot"] = {
            "total_cooks": stored_value(read(node, "totalCooks")),
            "last_cook_abs_frame": stored_value(read(node, "cookAbsFrame")),
        }
        for method in ("errors", "warnings", "scriptErrors"):
            try:
                message = getattr(node, method)()
                if message:
                    row.setdefault("diagnostics", {})[method] = shorten(message, 2000)
            except Exception as exc:
                gaps.append({"path": path, "field": method, "reason": type(exc).__name__})
        try:
            row["inputs"], row["component_ports"] = connection_data(node)
        except Exception as exc:
            gaps.append({"path": path, "field": "connections", "reason": type(exc).__name__})
        try:
            row["parameters"] = parameter_settings(node, enum, gaps)
        except Exception as exc:
            gaps.append({"path": path, "field": "parameters", "reason": type(exc).__name__})
        if node.family == "COMP":
            try:
                if read(node, "isPrivacyActive", False):
                    gaps.append({"path": path, "field": "children", "reason": "private component"})
                else:
                    todo.extend((child, path) for child in reversed(node.children))
            except Exception as exc:
                gaps.append({"path": path, "field": "children", "reason": type(exc).__name__})

    return {
        "schema": "oculus.td.network.v1",
        "exporter_version": "1.1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "role": role,
        "root_path": root.path,
        "project": Path(td.project.name).name,
        "touchdesigner_version": td.app.version,
        "touchdesigner_build": read(td.app, "build"),
        "process_id": os.getpid(),
        "operator_count": len(operators),
        "operator_type_counts": dict(Counter(row["type"] for row in operators)),
        "endpoint_candidates": [
            {"path": row["path"], "type": row["type"]}
            for row in operators if row["type"] in ENDPOINTS
        ],
        "excluded_system_roots": sorted(SYSTEM_ROOTS),
        "capture_gaps": gaps,
        "operators": operators,
    }


def export(role, scope, parameter_modes):
    import td

    if role not in {"TD_A", "TD_B"}:
        raise ValueError(f"Invalid TouchDesigner role: {role!r}")
    folder = Path(__file__).resolve().parent
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = folder / f"Oculus_{role}_network_{timestamp}.json"
    try:
        report = inventory(td, role, scope, parameter_modes)
        if not any(row.get("parameters") for row in report["operators"]) and any(
            gap["field"] in ("parameter", "parameters") for gap in report["capture_gaps"]
        ):
            raise RuntimeError("Parameter capture failed.")
        temporary = output.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(report, file, ensure_ascii=True, allow_nan=False, indent=2)
                file.write("\n")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    except Exception as exc:
        td.ui.status = f"Oculus network export failed: {type(exc).__name__}"
        raise
    td.ui.status = f"Exported {role}: {report['operator_count']} operators to {output.name}"
    print(f"Oculus network export: {output}")


if __name__ == "__main__":
    export(globals().get("ROLE"), globals().get("SCOPE", "/"), globals().get("PAR_MODE"))
