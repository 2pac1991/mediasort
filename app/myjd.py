"""Minimaler Client für die My.JDownloader-API (api.jdownloader.org), wie ihn auch die Handy-Apps nutzen.

Ablauf: mit E-Mail und Passwort anmelden (/my/connect), Gerät suchen (/my/listdevices) und die Links
über /linkgrabberv2/addLinks an dieses Gerät schicken. Alle Antworten sind AES-128-CBC-verschlüsselt."""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import config

API = os.environ.get("MYJD_API", "https://api.jdownloader.org")  # für Tests umstellbar
APP_KEY = "Mediasort"
TIMEOUT = 15
_lock = threading.Lock()
_rid = 0


class MyJDError(Exception):
    pass


def _next_rid():
    global _rid
    with _lock:
        _rid = max(_rid + 1, int(time.time() * 1000))
        return _rid


def _secret(email, password, domain):
    return hashlib.sha256(email.lower().encode() + password.encode() + domain.encode()).digest()


def _encrypt(token, text):
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(token[16:]), modes.CBC(token[:16])).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _decrypt(token, b64):
    dec = Cipher(algorithms.AES(token[16:]), modes.CBC(token[:16])).decryptor()
    data = dec.update(base64.b64decode(b64)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(data) + unpadder.finalize()).decode()


def _http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data.encode() if data else None, headers=headers or {},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)
            src, typ = err.get("src", ""), err.get("type", "")
        except ValueError:
            src, typ = "", ""
        if typ in ("AUTH_FAILED", "EMAIL_INVALID", "TOKEN_INVALID"):
            raise MyJDError("My.JDownloader: E-Mail oder Passwort falsch") from e
        if typ == "OFFLINE" or e.code == 503:
            raise MyJDError("My.JDownloader: Dein JDownloader ist gerade nicht online") from e
        raise MyJDError(f"My.JDownloader-Fehler {e.code} {typ or src}".strip()) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise MyJDError(f"My.JDownloader nicht erreichbar: {e}") from e


class Session:
    def __init__(self, email, password):
        self.login_secret = _secret(email, password, "server")
        self.device_secret = _secret(email, password, "device")
        rid = _next_rid()
        query = f"/my/connect?email={urllib.parse.quote(email)}&appkey={urllib.parse.quote(APP_KEY)}&rid={rid}"
        res = self._get(query, self.login_secret)
        self.token = res["sessiontoken"]
        raw = bytes.fromhex(self.token)
        self.server_token = hashlib.sha256(self.login_secret + raw).digest()
        self.device_token = hashlib.sha256(self.device_secret + raw).digest()

    def _get(self, query, key):
        sig = hmac.new(key, query.encode(), hashlib.sha256).hexdigest()
        return json.loads(_decrypt(key, _http(API + query + "&signature=" + sig)))

    def devices(self):
        rid = _next_rid()
        res = self._get(f"/my/listdevices?sessiontoken={urllib.parse.quote(self.token)}&rid={rid}", self.server_token)
        return [{"id": d["id"], "name": d["name"]} for d in res.get("list", [])]

    def call(self, device_id, path, params):
        body = json.dumps({"url": path, "params": [json.dumps(p) for p in params], "rid": _next_rid(), "apiVer": 1})
        url = f"{API}/t_{urllib.parse.quote(self.token)}_{urllib.parse.quote(device_id)}{path}"
        raw = _http(url, _encrypt(self.device_token, body), {"Content-Type": "application/aesjson-jd; charset=utf-8"})
        return json.loads(_decrypt(self.device_token, raw)).get("data")

    def disconnect(self):
        try:
            self._get(f"/my/disconnect?sessiontoken={urllib.parse.quote(self.token)}&rid={_next_rid()}", self.server_token)
        except Exception:  # noqa: BLE001
            pass


def configured():
    s = config.get()
    return bool(s.get("myjd_email") and s.get("myjd_password"))


def _session():
    s = config.get()
    if not configured():
        raise MyJDError("My.JDownloader ist nicht eingerichtet")
    return Session(s["myjd_email"].strip(), s["myjd_password"]), s


def list_devices():
    sess, _ = _session()
    try:
        return sess.devices()
    finally:
        sess.disconnect()


def add_links(urls, package=""):
    sess, s = _session()
    try:
        devices = sess.devices()
        if not devices:
            raise MyJDError("My.JDownloader: Kein JDownloader mit diesem Konto online")
        wanted = (s.get("myjd_device") or "").strip()
        dev = next((d for d in devices if d["name"] == wanted), None) if wanted else devices[0]
        if not dev:
            raise MyJDError(f"My.JDownloader: Gerät „{wanted}“ ist nicht online")
        sess.call(dev["id"], "/linkgrabberv2/addLinks", [{
            "links": "\n".join(urls),
            "packageName": package or None,
            "autostart": True,
            "overwritePackagizerRules": False,
            "priority": "DEFAULT",
            "destinationFolder": None,
            "extractPassword": None,
            "downloadPassword": None,
        }])
        return dev["name"]
    finally:
        sess.disconnect()
