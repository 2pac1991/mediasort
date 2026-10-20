"""HTTP-Server mit JSON-API und Web-Oberfläche (nur Python-Standardbibliothek)."""
import base64
import hmac
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, db, renamer, service, tmdb

VERSION = "1.0.0"
STATIC = os.path.join(os.path.dirname(__file__), "static")
PASSWORD = os.environ.get("UI_PASSWORD", "")
log = logging.getLogger("mediasort")


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
    s["tmdb_api_key"] = ""
    s["plex_token"] = ""
    return s


class Handler(BaseHTTPRequestHandler):
    server_version = "Mediasort/" + VERSION

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---------- Helfer ----------
    def _auth_ok(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                return hmac.compare_digest(pw, PASSWORD)
            except Exception:  # noqa: BLE001
                return False
        return False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _guard(self):
        if self._auth_ok():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Mediasort"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # ---------- Routing ----------
    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
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
        except Exception as e:  # noqa: BLE001
            log.exception("GET %s", self.path)
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            body = self._json()
            if u.path == "/api/settings":
                if not body.get("tmdb_api_key"):
                    body.pop("tmdb_api_key", None)
                if not body.get("plex_token") and not body.get("clear_plex_token"):
                    body.pop("plex_token", None)
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
        except (OSError, ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
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
    port = int(os.environ.get("PORT", "8484"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("Mediasort %s läuft auf Port %d", VERSION, port)
    try:
        httpd.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
