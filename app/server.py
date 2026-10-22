"""HTTP-Server mit JSON-API und Web-Oberfläche (nur Python-Standardbibliothek)."""
import json
import logging
import time
import os
import re
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import adblock, config, db, files, library, links, myjd, rbrowser, renamer, service, tmdb, users

VERSION = "1.2.0"
STATIC = os.path.join(os.path.dirname(__file__), "static")
# Früherer Einzel-Passwortschutz: wenn gesetzt, muss er beim Anlegen des ersten Admins angegeben werden
SETUP_CODE = os.environ.get("UI_PASSWORD", "")
COOKIE = "mediasort_session"
# Hängendes JDownloader-Paket, das jemand gerade im Browser löst: wird nach erfolgreichem Senden gelöscht
PENDING = {}
log = logging.getLogger("mediasort")

# Statische Dateien für Icon und Web-App (ohne Anmeldung, sonst kann das Handy sie nicht laden)
STATIC_FILES = {
    "/icon.png": "image/png", "/icon-192.png": "image/png", "/icon-512.png": "image/png",
    "/icon-maskable-512.png": "image/png", "/apple-touch-icon.png": "image/png",
    "/manifest.webmanifest": "application/manifest+json",
    "/sw.js": "text/javascript; charset=utf-8", "/offline.html": "text/html; charset=utf-8",
}
# Ohne Anmeldung erreichbar
PUBLIC = {"/adblock.pac", "/", "/index.html", "/share", "/cnl", "/api/health", "/api/auth/me", "/api/auth/login", "/api/auth/setup",
          "/api/auth/logout", *STATIC_FILES}


def retarget_open_items():
    """Nach Änderung der Einstellungen Ziele neu berechnen (ohne TMDB-Abfrage)."""
    s = config.get()
    for it in db.list_items():
        if it.get("match"):
            it["target"] = renamer.target_for(it["parsed"], it["match"], s)
            db.upsert_item(it)


def public_settings():
    s = config.get()
    s["tmdb_api_key_set"] = bool(s.get("tmdb_api_key"))
    s["plex_token_set"] = bool(s.get("plex_token"))
    s["myjd_password_set"] = bool(s.get("myjd_password"))
    s["tmdb_api_key"] = ""
    s["plex_token"] = ""
    s["myjd_password"] = ""
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = "Mediasort/" + VERSION

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---------- Helfer ----------
    def _user(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return users.check_token(c[COOKIE].value) if COOKIE in c else None

    def _send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if cookie is not None:
            age = users.SESSION_DAYS * 86400 if cookie else 0
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Max-Age={age}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _guard(self, path, post=False):
        """Setzt self.me; liefert False (und hat schon geantwortet), wenn der Zugriff nicht erlaubt ist."""
        # Schutz gegen Formular-Posts von fremden Seiten: API-Aufrufe kommen immer als JSON
        if post and not self.headers.get("Content-Type", "").startswith("application/json"):
            self._send(415, {"error": "JSON erwartet"})
            return False
        self.me = self._user()
        if path in PUBLIC or self.me:
            return True
        self._send(401, {"error": "Bitte anmelden", "setup": not users.has_users()})
        return False

    def _need(self, admin=False, delete=False, add_links=False):
        if admin and self.me["role"] != "admin":
            raise PermissionError("Nur für Admins")
        if delete and not self.me["can_delete"]:
            raise PermissionError("Du darfst nichts löschen")
        if add_links and not self.me["can_links"]:
            raise PermissionError("Du darfst keine Links senden")

    # ---------- Routing ----------
    def do_GET(self):
        u = urlparse(self.path)
        if not self._guard(u.path):
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html", "/share", "/cnl"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path in STATIC_FILES:
                with open(os.path.join(STATIC, u.path.lstrip("/")), "rb") as f:
                    return self._send(200, f.read(), STATIC_FILES[u.path])
            if u.path == "/api/health":
                return self._send(200, {"ok": True})
            if u.path == "/adblock.pac":
                # Nur für den eingebauten Browser auf demselben Rechner
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    return self._send(404, {"error": "Nicht gefunden"})
                return self._send(200, adblock.pac(), "application/x-ns-proxy-autoconfig")
            if u.path == "/api/auth/me":
                me = dict(self.me, webhook_url=users.get_webhook(self.me["name"])) if self.me else None
                return self._send(200, {"user": me, "setup": not users.has_users()})
            if u.path == "/api/users":
                self._need(admin=True)
                return self._send(200, users.list_users())
            if u.path == "/api/files/roots":
                return self._send(200, [{"key": k, "name": n} for k, (n, _) in files.roots().items()])
            if u.path == "/api/files":
                return self._send(200, files.list_dir(q.get("root", ""), q.get("path", "")))
            if u.path == "/api/links":
                self._need(add_links=True)
                return self._send(200, {"items": db.list_links(), "configured": links.configured(), "live": myjd.configured(),
                                       "browser": rbrowser.available()})
            if u.path == "/api/rb/poll":
                self._need(add_links=True)
                return self._send(200, rbrowser.BROWSER.poll(self.me["name"], int(q.get("after") or 0)))
            if u.path == "/api/jd/status":
                self._need(add_links=True)
                try:
                    if config.get().get("jd_skip_existing"):
                        myjd.remove_existing()
                    st = myjd.status()
                    # Dateien der aufgeklappten Pakete gleich mitliefern
                    for kind in ("grabber", "downloads"):
                        ids = [i for i in q.get("open_" + kind, "").split(",") if i.isdigit()]
                        st["links_" + kind] = myjd.links(kind, ids) if ids else []
                    for l in st["links_grabber"]:
                        l["existing"] = library.exists(l.get("name") or "")
                    st["removed"] = myjd.recently_removed()[:5]
                    return self._send(200, st)
                except myjd.MyJDError as e:
                    raise ValueError(str(e)) from e
            if u.path == "/api/trash":
                return self._send(200, {"items": files.list_trash(), "days": config.get().get("trash_days")})
            if u.path == "/api/status":
                s = config.get()
                return self._send(200, {
                    "version": VERSION, "stats": db.stats(), "state": service.state,
                    "tmdb_key": bool(s.get("tmdb_api_key")), "watch_enabled": s.get("watch_enabled"),
                    "input_dirs": [{"path": d, "exists": os.path.isdir(d)} for d in s["input_dirs"]],
                })
            if u.path == "/api/settings":
                return self._send(200, public_settings())
            if u.path == "/api/items":
                return self._send(200, db.list_items(include_done=q.get("all") == "1"))
            if u.path == "/api/history":
                return self._send(200, db.list_history(int(q.get("limit", 200))))
            if u.path == "/api/search":
                t, query = q.get("type", "movie"), q.get("q", "").strip()
                year = int(q["year"]) if q.get("year", "").isdigit() else None
                if not query:
                    return self._send(200, [])
                if query.isdigit() and len(query) > 2 and not year:
                    one = tmdb.movie(int(query)) if t == "movie" else tmdb.tv(int(query))
                    return self._send(200, [one] if one else [])
                res = tmdb.search_movie(query, year) if t == "movie" else tmdb.search_tv(query, year)
                return self._send(200, res[:15])
            m = re.fullmatch(r"/api/tv/(\d+)", u.path)
            if m:
                return self._send(200, tmdb.tv(int(m.group(1))))
            return self._send(404, {"error": "Nicht gefunden"})
        except tmdb.TMDBError as e:
            return self._send(502, {"error": str(e)})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except (OSError, ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s", self.path)
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._guard(u.path, post=True):
            return
        try:
            body = self._json()
            # ---------- Anmeldung ----------
            if u.path == "/api/auth/setup":
                if users.has_users():
                    raise PermissionError("Es gibt schon einen Admin")
                if SETUP_CODE and body.get("setup_code") != SETUP_CODE:
                    raise PermissionError("Falscher Setup-Code (UI_PASSWORD)")
                me = users.save_user(body.get("name"), body.get("password"), role="admin", can_delete=True, create=True)
                return self._send(200, {"user": me}, cookie=users.make_token(me["name"]))
            if u.path == "/api/auth/login":
                name = users.verify(body.get("name"), body.get("password"), self.client_address[0])
                if not name:
                    return self._send(401, {"error": "Name oder Passwort falsch"})
                return self._send(200, {"user": users.get(name)}, cookie=users.make_token(name))
            if u.path == "/api/auth/logout":
                return self._send(200, {"ok": True}, cookie="")
            if u.path == "/api/auth/password":
                users._check_password(body.get("password"))
                if not users.verify(self.me["name"], body.get("old_password"), self.client_address[0]):
                    raise PermissionError("Altes Passwort stimmt nicht")
                me = users.save_user(self.me["name"], body.get("password"), self.me["role"], self.me["can_delete"])
                return self._send(200, {"user": me}, cookie=users.make_token(me["name"]))
            # ---------- Eigene Benachrichtigung ----------
            if u.path == "/api/me/webhook":
                users.set_webhook(self.me["name"], body.get("url"))
                return self._send(200, {"ok": True, "webhook_url": users.get_webhook(self.me["name"])})
            if u.path == "/api/me/webhook/test":
                url = users.get_webhook(self.me["name"])
                if not url:
                    raise ValueError("Keine Webhook-Adresse hinterlegt")
                text = "Dark (2017) – S01E03 Vergangenheit und Gegenwart (Test von Mediasort)"
                errors = service.send_webhooks([url], {"count": 1, "files": ["Dark (2017) - s01e03.mkv"], "text": text, "items": [{
                    "kind": "series", "title": "Dark", "year": 2017, "season": 1, "episode": 3,
                    "eptitle": "Vergangenheit und Gegenwart", "file": "Dark (2017) - s01e03.mkv", "text": text,
                    "poster": "https://image.tmdb.org/t/p/w500/apbrbWs8M9lyOpJYU5WXrpFbk1Z.jpg"}]}, wait=True)
                if errors:
                    raise ValueError("Nicht erreichbar: " + next(iter(errors.values())))
                return self._send(200, {"ok": True})
            # ---------- Nutzerverwaltung (Admin) ----------
            if u.path == "/api/users":
                self._need(admin=True)
                return self._send(200, users.save_user(
                    body.get("name"), body.get("password") or None, body.get("role", "user"),
                    body.get("can_delete", False), create=bool(body.get("create")), can_links=body.get("can_links")))
            if u.path == "/api/users/delete":
                self._need(admin=True)
                if body.get("name") == self.me["name"]:
                    raise ValueError("Du kannst dich nicht selbst löschen")
                users.delete_user(body.get("name"))
                return self._send(200, {"ok": True})
            # ---------- Links an JDownloader ----------
            if u.path == "/api/links":
                self._need(add_links=True)
                n = links.send(body.get("text"), body.get("package"), self.me["name"])
                log.info("%s hat %d Link(s) an JDownloader geschickt", self.me["name"], n)
                cleaned = None
                pend = PENDING.pop(self.me["name"], None)
                if pend and time.time() - pend["ts"] < 1800 and myjd.configured():
                    try:
                        myjd.remove(pend["kind"], [], [pend["uuid"]])
                        cleaned = pend["name"]
                    except myjd.MyJDError as e:
                        log.warning("Hängendes Paket nicht gelöscht: %s", e)
                return self._send(200, {"ok": True, "count": n, "cleaned": cleaned})
            if u.path == "/api/jd/pending":
                self._need(add_links=True)
                PENDING[self.me["name"]] = {"kind": "grabber" if body.get("kind") == "grabber" else "downloads",
                                            "uuid": int(body["uuid"]), "name": body.get("name", ""), "ts": time.time()}
                return self._send(200, {"ok": True})
            if u.path in ("/api/jd/start", "/api/jd/control"):
                self._need(add_links=True)
                try:
                    if u.path == "/api/jd/start":
                        myjd.start_packages(body.get("ids") or [])
                    else:
                        myjd.control(body.get("action"))
                    return self._send(200, {"ok": True})
                except myjd.MyJDError as e:
                    raise ValueError(str(e)) from e
            if u.path == "/api/jd/remove":
                self._need(add_links=True)
                try:
                    kind = "grabber" if body.get("kind") == "grabber" else "downloads"
                    myjd.remove(kind, body.get("links") or [], body.get("packages") or [])
                    log.info("%s hat im JDownloader gelöscht: %s", self.me["name"], body.get("name", ""))
                    return self._send(200, {"ok": True})
                except myjd.MyJDError as e:
                    raise ValueError(str(e)) from e
            # ---------- Eingebauter Browser ----------
            if u.path.startswith("/api/rb/"):
                self._need(add_links=True)
                b, name = rbrowser.BROWSER, self.me["name"]
                try:
                    if u.path == "/api/rb/open":
                        url = b.open(name, body.get("url"), body.get("width", 390), body.get("height", 700), body.get("mobile", True))
                        log.info("%s öffnet im eingebauten Browser: %s", name, url)
                        return self._send(200, {"ok": True, "url": url})
                    if u.path == "/api/rb/input":
                        b.input(name, body)
                    elif u.path == "/api/rb/nav":
                        b.nav(name, body.get("action"), body.get("url", ""))
                    elif u.path == "/api/rb/resize":
                        b.resize(name, body.get("width", 390), body.get("height", 700))
                    elif u.path == "/api/rb/grab":
                        return self._send(200, b.grab(name))
                    elif u.path == "/api/rb/close":
                        b.close(name)
                    else:
                        return self._send(404, {"error": "Unbekannt"})
                    return self._send(200, {"ok": True})
                except rbrowser.BrowserError as e:
                    raise ValueError(f"Eingebauter Browser: {e}") from e
            if u.path == "/api/cnl/decode":
                self._need(add_links=True)
                if body.get("keys") and body.get("crypts"):
                    found = links.decode_candidates(body["keys"].split(","), body["crypts"].split(","))
                else:
                    found = links.decode_cnl(body.get("crypted", ""), body.get("jk", ""), body.get("urls", ""))
                return self._send(200, {"urls": found, "package": body.get("package") or links.guess_package(found)})
            # ---------- Dateien ----------
            if u.path == "/api/files/delete":
                self._need(delete=True)
                tid = files.delete(body.get("root", ""), body.get("path", ""), self.me["name"])
                log.info("%s hat %s/%s gelöscht (Papierkorb #%s)", self.me["name"], body.get("root"), body.get("path"), tid)
                return self._send(200, {"ok": True, "trash_id": tid})
            if u.path == "/api/files/rename":
                name = files.rename(body.get("root", ""), body.get("path", ""), body.get("name"), self.me["name"])
                return self._send(200, {"ok": True, "name": name})
            if u.path == "/api/trash/restore":
                return self._send(200, {"ok": True, "path": files.restore(int(body["id"]))})
            if u.path == "/api/trash/purge":
                self._need(admin=True)
                ids = [t["id"] for t in db.list_trash()] if body.get("all") else [int(body["id"])]
                for tid in ids:
                    files.purge(tid)
                return self._send(200, {"ok": True})
            # ---------- Einsortieren ----------
            if u.path == "/api/myjd/test":
                self._need(admin=True)
                try:
                    return self._send(200, {"devices": myjd.list_devices()})
                except myjd.MyJDError as e:
                    raise ValueError(str(e)) from e
            if u.path in ("/api/settings", "/api/test-tmdb", "/api/cache/clear"):
                self._need(admin=True)
            if u.path == "/api/settings":
                if not body.get("tmdb_api_key"):
                    body.pop("tmdb_api_key", None)
                if not body.get("plex_token") and not body.get("clear_plex_token"):
                    body.pop("plex_token", None)
                if not body.get("myjd_password") and not body.get("clear_myjd"):
                    body.pop("myjd_password", None)
                if body.get("clear_myjd"):
                    body.update(myjd_email="", myjd_password="", myjd_device="")
                for key in ("input_dirs", "anime_keywords"):
                    if isinstance(body.get(key), str):
                        body[key] = [x.strip() for x in body[key].splitlines() if x.strip()]
                config.save(body)
                retarget_open_items()
                return self._send(200, public_settings())
            if u.path == "/api/test-tmdb":
                tmdb.test()
                return self._send(200, {"ok": True})
            if u.path == "/api/scan":
                force = bool(body.get("force"))
                if service.scan_lock.locked():
                    return self._send(409, {"error": "Scan läuft bereits"})
                n = service.scan(force=force)
                return self._send(200, {"new": n, "items": db.list_items()})
            if u.path == "/api/apply":
                return self._send(200, service.apply_items(body.get("ids") or []))
            m = re.fullmatch(r"/api/items/(\w+)/match", u.path)
            if m:
                it = service.rematch(m.group(1), body.get("type"), int(body["id"]), body.get("season"), body.get("episode"))
                return self._send(200, it)
            m = re.fullmatch(r"/api/items/(\w+)/(ignore|reset)", u.path)
            if m:
                db.set_status(m.group(1), "ignored" if m.group(2) == "ignore" else "review", None)
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/api/history/(\d+)/undo", u.path)
            if m:
                entry = db.get_history(int(m.group(1)))
                if not entry or entry["undone"]:
                    return self._send(404, {"error": "Eintrag nicht gefunden oder schon rückgängig"})
                renamer.undo(entry, config.get())
                iid = service.item_id(entry["src"])
                if db.get_item(iid):
                    db.set_status(iid, "review", "Rückgängig gemacht")
                return self._send(200, {"ok": True})
            if u.path == "/api/cache/clear":
                db.cache_clear()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "Nicht gefunden"})
        except tmdb.TMDBError as e:
            return self._send(502, {"error": str(e)})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except FileExistsError as e:
            return self._send(409, {"error": str(e)})
        except (OSError, ValueError, KeyError) as e:
            return self._send(400, {"error": getattr(e, "strerror", None) or str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s", self.path)
            return self._send(500, {"error": str(e)})


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    config.load()
    db.init(os.path.join(config.CONFIG_DIR, "mediasort.db"))
    stop = threading.Event()
    threading.Thread(target=service.watch_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=rbrowser.housekeeping_loop, args=(stop,), daemon=True).start()
    port = int(os.environ.get("PORT", "8484"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("Mediasort %s läuft auf Port %d", VERSION, port)
    try:
        httpd.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
