import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import stat

MAX_RESPONSE = 4096


class PiKVMError(Exception):
    pass


def credentials(username, password, fingerprint):
    fingerprint = fingerprint.rsplit("=", 1)[-1].strip().replace(":", "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise PiKVMError("invalid_certificate_fingerprint")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username) or not 1 <= len(password) <= 256:
        raise PiKVMError("invalid_web_credentials")
    return {"username": username, "password": password, "fingerprint": fingerprint}


class PiKVM:
    def __init__(self, ip, source_ip, path, port=443):
        self.ip = ip
        self.source_ip = source_ip
        self.path = path
        self.port = port

    def read_credentials(self):
        try:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_RESPONSE):
                raise PiKVMError("credentials_permissions")
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return credentials(value["username"], value["password"], value["fingerprint"])
        except FileNotFoundError as exc:
            raise PiKVMError("credentials_missing") from exc
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise PiKVMError("credentials_invalid") from exc

    def request(self, method, path, auth):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            self.ip, self.port, timeout=2, context=context, source_address=(self.source_ip, 0)
        )
        try:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, auth["fingerprint"]):
                raise PiKVMError("certificate_mismatch")
            token = base64.b64encode((auth["username"] + ":" + auth["password"]).encode("utf-8")).decode("ascii")
            connection.request(method, path, headers={
                "Authorization": "Basic " + token, "Accept": "application/json", "Connection": "close",
            })
            with connection.getresponse() as response:
                if response.status in (401, 403):
                    raise PiKVMError("web_auth_rejected")
                if response.status != 200:
                    raise PiKVMError("http_" + str(response.status))
                data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise PiKVMError("response_too_large")
            obj = json.loads(data)
            if type(obj) is not dict or obj.get("ok") is not True or type(obj.get("result")) is not dict:
                raise PiKVMError("invalid_api_result")
            return obj["result"]
        except TimeoutError as exc:
            raise PiKVMError("timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise PiKVMError("connection_or_tls_error") from exc
        except (ValueError, RecursionError) as exc:
            raise PiKVMError("invalid_api_result") from exc
        finally:
            connection.close()

    def probe(self, auth=None):
        try:
            value = self.request("GET", "/api/atx", self.read_credentials() if auth is None else auth)
            enabled, busy, power = value["enabled"], value["busy"], value["leds"]["power"]
            if any(type(item) is not bool for item in (enabled, busy, power)):
                raise PiKVMError("invalid_atx_fields")
            return dict(state="reachable", enabled=enabled, busy=busy, power_led=power)
        except PiKVMError as exc:
            return dict(state="unavailable", reason=str(exc), enabled=None, busy=None, power_led=None)
        except (KeyError, TypeError):
            return dict(state="unavailable", reason="invalid_atx_fields", enabled=None, busy=None, power_led=None)


def atx_ready(atx, powered=True):
    return (atx.get("state") == "reachable" and atx.get("enabled") is True
            and atx.get("busy") is False and atx.get("power_led") is powered)
