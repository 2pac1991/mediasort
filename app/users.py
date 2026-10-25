"""Nutzerkonten und Anmeldung: Passwörter als PBKDF2-Hash, Sitzung als signiertes Cookie."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import config

USERS_FILE = os.path.join(config.CONFIG_DIR, "users.json")
SECRET_FILE = os.path.join(config.CONFIG_DIR, "secret.key")
SESSION_DAYS = 30
ITERATIONS = 200_000
ROLES = ("admin", "user")

_lock = threading.Lock()
_users = None
_secret = None
_fails = {}  # IP -> [Zeitpunkte fehlgeschlagener Logins]


def _load():
    global _users
    if _users is None:
        try:
            with open(USERS_FILE, encoding="utf-8") as f:
                _users = json.load(f)
        except (OSError, ValueError):
            _users = {}
    return _users


def _save():
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_users, f, indent=2, ensure_ascii=False)
    os.replace(tmp, USERS_FILE)


def _get_secret():
    global _secret
    if _secret is None:
        try:
            with open(SECRET_FILE, "rb") as f:
                _secret = f.read()
        except OSError:
            _secret = secrets.token_bytes(32)
            os.makedirs(config.CONFIG_DIR, exist_ok=True)
            with open(SECRET_FILE, "wb") as f:
                f.write(_secret)
    return _secret


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()


def _clean_name(name):
    name = (name or "").strip()
    if not name or len(name) > 40 or any(c in name for c in '|:/\\"<>'):
        raise ValueError("Ungültiger Benutzername")
    return name


def _check_password(password):
    if not password or len(password) < 6:
        raise ValueError("Passwort muss mindestens 6 Zeichen haben")


def public(name, u):
    admin = u["role"] == "admin"
    return {"name": name, "role": u["role"], "can_delete": admin or bool(u.get("can_delete")),
            "can_links": admin or bool(u.get("can_links"))}


def has_users():
    with _lock:
        return bool(_load())


def list_users():
    with _lock:
        return [public(n, u) for n, u in sorted(_load().items(), key=lambda x: x[0].lower())]


def get(name):
    with _lock:
        u = _load().get(name)
        return public(name, u) if u else None


def save_user(name, password=None, role="user", can_delete=False, create=False, can_links=None):
    name = _clean_name(name)
    if role not in ROLES:
        raise ValueError("Ungültige Rolle")
    with _lock:
        users = _load()
        if create and name in users:
            raise ValueError("Diesen Benutzer gibt es schon")
        if not create and name not in users:
            raise KeyError(name)
        u = users.get(name, {})
        if create or password:
            _check_password(password)
            u["salt"] = secrets.token_hex(16)
            u["hash"] = _hash(password, u["salt"])
        if u.get("role") == "admin" and role != "admin" and _admin_count(users) <= 1:
            raise ValueError("Der letzte Admin kann nicht herabgestuft werden")
        u["role"], u["can_delete"] = role, bool(can_delete)
        if can_links is not None:
            u["can_links"] = bool(can_links)
        users[name] = u
        _save()
        return public(name, u)


def _admin_count(users):
    return sum(1 for u in users.values() if u.get("role") == "admin")


def delete_user(name):
    with _lock:
        users = _load()
        if name not in users:
            raise KeyError(name)
        if users[name].get("role") == "admin" and _admin_count(users) <= 1:
            raise ValueError("Der letzte Admin kann nicht gelöscht werden")
        del users[name]
        _save()


def verify(name, password, ip=""):
    """Prüft die Anmeldung, mit Bremse gegen Durchprobieren (10 Fehlversuche / 10 Min. je IP)."""
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < 600]
        _fails[ip] = recent
        if len(recent) >= 10:
            raise PermissionError("Zu viele Fehlversuche, bitte in ein paar Minuten nochmal")
        u = _load().get((name or "").strip())
        ok = bool(u) and hmac.compare_digest(_hash(password or "", u["salt"]), u["hash"])
        if not ok:
            _fails[ip].append(now)
            return None
        _fails.pop(ip, None)
        return (name or "").strip()


def _sign(payload, u):
    # Der Passwort-Hash fließt mit ein: Passwort ändern oder Konto löschen beendet alle Sitzungen
    return hmac.new(_get_secret(), (payload + "|" + u["hash"]).encode(), hashlib.sha256).hexdigest()


def make_token(name):
    with _lock:
        u = _load()[name]
        payload = f"{name}|{int(time.time()) + SESSION_DAYS * 86400}"
        raw = payload + "|" + _sign(payload, u)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def check_token(token):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        name, exp, sig = raw.rsplit("|", 2)
        if int(exp) < time.time():
            return None
        with _lock:
            u = _load().get(name)
            if not u or not hmac.compare_digest(sig, _sign(f"{name}|{exp}", u)):
                return None
            return public(name, u)
    except Exception:  # noqa: BLE001
        return None


def set_webhook(name, url):
    url = (url or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Die Adresse muss mit http:// oder https:// beginnen")
    with _lock:
        users = _load()
        if name not in users:
            raise KeyError(name)
        if url:
            users[name]["webhook_url"] = url
        else:
            users[name].pop("webhook_url", None)
        _save()


def get_webhook(name):
    with _lock:
        return (_load().get(name) or {}).get("webhook_url", "")


def all_webhooks():
    with _lock:
        return [u["webhook_url"] for u in _load().values() if u.get("webhook_url")]


def link_webhooks():
    """Webhooks aller, die Links senden dürfen (Admins immer): die bekommen Captcha-Hinweise."""
    with _lock:
        return [u["webhook_url"] for u in _load().values()
                if u.get("webhook_url") and (u.get("role") == "admin" or u.get("can_links"))]
