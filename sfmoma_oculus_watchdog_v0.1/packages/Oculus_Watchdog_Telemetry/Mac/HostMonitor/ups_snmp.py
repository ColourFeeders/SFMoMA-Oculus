import asyncio
from getpass import getpass
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


POWER_STATES = {
    1: "other", 2: "off", 3: "utility", 4: "bypass",
    5: "battery", 6: "boost", 7: "trim",
}
QUERY_ERRORS = {"snmp_no_response", "snmp_authentication", "snmp_unsupported_source",
                "snmp_dependency_missing", "snmp_error"}
SOURCE_OID = "1.3.6.1.2.1.33.1.4.1.0"
BATTERY_SECONDS_OID = "1.3.6.1.2.1.33.1.2.2.0"
INPUT_EVENTS_OID = "1.3.6.1.2.1.33.1.3.1.0"
COUNTERS = ("battery_seconds", "input_events")


def credentials(host, username, auth_pass, priv_pass):
    address = IPv4Address(host)
    if address.is_unspecified or address.is_multicast:
        raise ValueError("UPS address must be unicast.")
    if not (type(username) is str and 1 <= len(username) <= 32
            and all(type(secret) is str and 8 <= len(secret) <= 128 for secret in (auth_pass, priv_pass))):
        raise ValueError("UPS SNMPv3 username or passphrase length is invalid.")
    return dict(host=str(address), username=username, auth_pass=auth_pass, priv_pass=priv_pass)


def prompt_credentials(host):
    username = input("UPS read-only SNMPv3 username [readonly]: ").strip() or "readonly"
    return credentials(host, username, getpass("Authentication passphrase: "), getpass("AES privacy passphrase: "))


class UPSProbe:
    def __init__(self, directory):
        self.path = directory / "ups-snmp.credentials.json"
        self.python = directory.parent / "ups-venv/bin/python"

    def read(self, settings=None):
        if settings is None:
            try:
                mode = self.path.lstat()
                if (not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid()
                        or mode.st_mode & 0o077 or mode.st_size > 2048):
                    return {"state": "unknown", "reason": "ups_credentials_permissions"}
                settings = credentials(**json.loads(self.path.read_text(encoding="utf-8")))
            except FileNotFoundError:
                return {"state": "unconfigured"}
            except (OSError, TypeError, ValueError, RecursionError):
                return {"state": "unknown", "reason": "ups_credentials_invalid"}
        try:
            result = subprocess.run(
                [str(self.python), "-I", str(Path(__file__).resolve())],
                input=json.dumps(settings), text=True, capture_output=True, timeout=7, check=True,
            )
            value = json.loads(result.stdout)
        except FileNotFoundError:
            return {"state": "unknown", "reason": "snmp_python_missing"}
        except subprocess.TimeoutExpired:
            return {"state": "unknown", "reason": "snmp_no_response"}
        except (OSError, ValueError, subprocess.CalledProcessError, RecursionError):
            return {"state": "unknown", "reason": "snmp_error"}
        if type(value) is dict:
            code = value.get("source_code")
            if type(code) is int and code in POWER_STATES:
                reading = {"state": POWER_STATES[code], "source_code": code}
                for name in COUNTERS:
                    count = value.get(name)
                    if type(count) is int and 0 <= count < 2**32:
                        reading[name] = count
                return reading
        return {"state": "unknown", "reason": value if type(value) is str and value in QUERY_ERRORS else "snmp_error"}


async def query(settings):
    from pysnmp.hlapi.v3arch.asyncio import (
        ContextData, ObjectIdentity, ObjectType, SnmpEngine,
        UdpTransportTarget, UsmUserData, get_cmd,
        USM_AUTH_HMAC192_SHA256, USM_PRIV_CFB128_AES,
    )

    user = UsmUserData(settings["username"], settings["auth_pass"], settings["priv_pass"],
                       USM_AUTH_HMAC192_SHA256, USM_PRIV_CFB128_AES)
    target = await UdpTransportTarget.create((settings["host"], 161), timeout=2, retries=1)
    engine = SnmpEngine()
    context = ContextData()

    async def get(*oids):
        objects = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        return await get_cmd(engine, user, target, context, *objects, lookupMib=False)

    error, status, _, values = await get(SOURCE_OID, BATTERY_SECONDS_OID, INPUT_EVENTS_OID)
    if not error and status:
        error, status, _, values = await get(SOURCE_OID)
    if error:
        message = str(error).lower()
        if "timeout" in message or "no snmp response" in message:
            return "snmp_no_response"
        if any(word in message for word in ("user", "authentication", "decrypt", "digest")):
            return "snmp_authentication"
        return "snmp_error"
    if status or not values:
        return "snmp_unsupported_source"
    result = {}
    for name, item in zip(("source_code", *COUNTERS), values):
        try:
            result[name] = int(item[1])
        except (ValueError, TypeError):
            continue
    return result if "source_code" in result else "snmp_unsupported_source"


if __name__ == "__main__":
    try:
        result = asyncio.run(query(credentials(**json.load(sys.stdin))))
    except ImportError:
        result = "snmp_dependency_missing"
    except Exception:
        result = "snmp_error"
    print(json.dumps(result))
