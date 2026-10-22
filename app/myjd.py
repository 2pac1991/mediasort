"""Minimaler Client für die My.JDownloader-API (api.jdownloader.org), wie ihn auch die Handy-Apps nutzen.

Ablauf: mit E-Mail und Passwort anmelden (/my/connect), Gerät suchen (/my/listdevices) und die Links
über /linkgrabberv2/addLinks an dieses Gerät schicken. Alle Antworten sind AES-128-CBC-verschlüsselt."""
import base64
import hashlib
import hmac
import json
import os
import re
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


_cache = {"sess": None, "dev": None, "key": None, "ts": 0}
SESSION_TTL = 600  # Sitzung 10 Minuten wiederverwenden, damit das Live-Fenster nicht ständig neu anmeldet


def _session():
    s = config.get()
    if not configured():
        raise MyJDError("My.JDownloader ist nicht eingerichtet")
    return Session(s["myjd_email"].strip(), s["myjd_password"]), s


def _pick_device(sess, s):
    devices = sess.devices()
    if not devices:
        raise MyJDError("My.JDownloader: Kein JDownloader mit diesem Konto online")
    wanted = (s.get("myjd_device") or "").strip()
    dev = next((d for d in devices if d["name"] == wanted), None) if wanted else devices[0]
    if not dev:
        raise MyJDError(f"My.JDownloader: Gerät „{wanted}“ ist nicht online")
    return dev


def _device_call(path, params=()):
    """Aufruf am JDownloader mit zwischengespeicherter Sitzung; bei abgelaufener Sitzung einmal neu anmelden."""
    s = config.get()
    key = (s.get("myjd_email"), s.get("myjd_password"), s.get("myjd_device"))
    for attempt in (1, 2):
        with _lock:
            fresh = _cache["sess"] is None or _cache["key"] != key or time.time() - _cache["ts"] > SESSION_TTL
        if fresh or attempt == 2:
            sess, s = _session()
            dev = _pick_device(sess, s)
            with _lock:
                _cache.update(sess=sess, dev=dev, key=key, ts=time.time())
        try:
            return _cache["sess"].call(_cache["dev"]["id"], path, list(params))
        except MyJDError:
            if attempt == 2:
                raise
            with _lock:
                _cache["sess"] = None


def list_devices():
    sess, _ = _session()
    try:
        return sess.devices()
    finally:
        sess.disconnect()


def add_links(urls, package=""):
    _device_call("/linkgrabberv2/addLinks", [{
        "links": "\n".join(urls),
        "packageName": package or None,
        "autostart": True,
        "overwritePackagizerRules": False,
        "priority": "DEFAULT",
        "destinationFolder": None,
        "extractPassword": None,
        "downloadPassword": None,
    }])
    return _cache["dev"]["name"]


def status():
    """Linksammler, Downloads und Zustand des JDownloaders für das Live-Fenster."""
    grabber = _device_call("/linkgrabberv2/queryPackages", [{
        "bytesTotal": True, "childCount": True, "hosts": True, "saveTo": True, "status": True,
        "availableOnlineCount": True, "availableOfflineCount": True, "availableUnknownCount": True,
        "maxResults": -1, "startAt": 0,
    }]) or []
    downloads = _device_call("/downloadsV2/queryPackages", [{
        "bytesLoaded": True, "bytesTotal": True, "childCount": True, "enabled": True, "eta": True,
        "finished": True, "hosts": True, "running": True, "speed": True, "status": True, "saveTo": True,
        "maxResults": -1, "startAt": 0,
    }]) or []
    state = _device_call("/downloadcontroller/getCurrentState")
    # Hängende Pakete (Captcha, Schutzseite): Adresse der Seite mitliefern, damit man sie im Browser lösen kann
    for kind, pkgs in (("grabber", grabber), ("downloads", downloads)):
        for p in pkgs:
            if STUCK.search(p.get("status") or "") or any(PROTECTORS.search(h or "") for h in p.get("hosts") or []):
                urls = [l.get("url") for l in links(kind, [p["uuid"]]) if l.get("url")]
                if urls:
                    p["source_url"] = urls[0]
    return {"device": _cache["dev"]["name"], "state": state, "grabber": grabber, "downloads": downloads}


STUCK = re.compile(r"captcha|unsupported|crypt|schutz|protect", re.IGNORECASE)
PROTECTORS = re.compile(r"(^|\.)(filecrypt\.(cc|co|to)|linkcrypt\.ws|keeplinks\.(org|co)|safelinking\.net|ncrypt\.in|"
                        r"share-links\.biz|hide\.cx|relink\.(us|to)|dir\.cr|protect-links\.net|linksafe\.cc)$", re.IGNORECASE)


def start_packages(package_ids):
    """Pakete aus dem Linksammler in die Downloadliste schieben und Downloads starten."""
    ids = [int(i) for i in package_ids]
    _device_call("/linkgrabberv2/moveToDownloadlist", [[], ids])
    _device_call("/downloadcontroller/start")


def control(action):
    if action == "start":
        _device_call("/downloadcontroller/start")
    elif action == "stop":
        _device_call("/downloadcontroller/stop")
    elif action == "pause":
        _device_call("/downloadcontroller/pause", [True])
    elif action == "resume":
        _device_call("/downloadcontroller/pause", [False])
    else:
        raise ValueError("Unbekannte Aktion")


LINK_QUERY = {"bytesTotal": True, "bytesLoaded": True, "status": True, "availability": True, "host": True, "url": True,
              "finished": True, "running": True, "enabled": True, "maxResults": -1, "startAt": 0}
_removed = []  # zuletzt automatisch entfernte Dateien, für die Anzeige


def links(kind, package_ids):
    """Einzelne Dateien eines oder mehrerer Pakete (kind: grabber oder downloads)."""
    path = "/linkgrabberv2/queryLinks" if kind == "grabber" else "/downloadsV2/queryLinks"
    return _device_call(path, [dict(LINK_QUERY, packageUUIDs=[int(i) for i in package_ids])]) or []


def remove(kind, link_ids=(), package_ids=()):
    """Aus dem Linksammler bzw. der Downloadliste löschen. Bei unfertigen Downloads auch die Teildateien."""
    lids, pids = [int(i) for i in link_ids], [int(i) for i in package_ids]
    if kind == "grabber":
        _device_call("/linkgrabberv2/removeLinks", [lids, pids])
    else:
        _device_call("/downloadsV2/cleanup", [lids, pids, "DELETE_ALL", "REMOVE_LINKS_AND_DELETE_FILES", "SELECTED"])


def remove_existing():
    """Dateien im Linksammler entfernen, die schon in der Mediathek sind. Liefert die entfernten Namen."""
    from . import library
    packages = _device_call("/linkgrabberv2/queryPackages", [{"childCount": True, "maxResults": -1, "startAt": 0}]) or []
    if not packages:
        return []
    found = links("grabber", [p["uuid"] for p in packages])
    dupes = [(l, library.exists(l.get("name") or "")) for l in found]
    dupes = [(l, why) for l, why in dupes if why]
    if not dupes:
        return []
    remove("grabber", [l["uuid"] for l, _ in dupes])
    names = [f"{l.get('name')} ({why})" for l, why in dupes]
    with _lock:
        _removed[:0] = [{"ts": time.time(), "text": n} for n in names]
        del _removed[30:]
    return names


def recently_removed():
    with _lock:
        return list(_removed)
