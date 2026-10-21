"""Kernlogik: Ordner scannen, zuordnen, anwenden, Watch-Ordner-Schleife."""
import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

from . import config, db, files, matcher, parser, renamer, tmdb

log = logging.getLogger("mediasort")
scan_lock = threading.Lock()
state = {"watch_last_run": None, "watch_running": False, "scanning": False, "last_error": None}
INCOMPLETE = (".part", ".!qb", ".crdownload", ".tmp", ".mediasort-part", ".partial")


def item_id(path):
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def iter_videos(settings):
    min_size = int(settings.get("min_size_mb") or 0) * 1024 * 1024
    for root in settings["input_dirs"]:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if name.startswith(".") or name.lower().endswith(INCOMPLETE):
                    continue
                if not parser.is_video(name) or parser.is_sample(full):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size < min_size:
                    continue
                yield full, st


def evaluate(path, st, settings, parsed=None, info=None, candidates=None):
    """Erkennen + zuordnen + Ziel berechnen. Liefert das Item-dict."""
    parsed = parsed or parser.parse(path, settings.get("anime_keywords") or ())
    item = {"id": item_id(path), "path": path, "size": st.st_size, "mtime": st.st_mtime, "parsed": parsed}
    try:
        if info is None:
            info, conf, cands = matcher.match(parsed)
        else:
            conf, cands = 1.0, candidates or []
        item["match"] = info
        if info:
            info["candidates"] = [
                {k: c.get(k) for k in ("type", "id", "title", "year", "poster", "score")} for c in (cands or [])
            ]
        item["confidence"] = conf
        item["target"] = renamer.target_for(parsed, info, settings)
        if not info:
            item["status"], item["message"] = "review", "Kein Treffer bei TMDB"
        elif info.get("error"):
            item["status"], item["message"] = "review", info["error"]
        elif not item["target"]:
            item["status"], item["message"] = "review", "Staffel/Folge unklar"
        elif conf >= float(settings.get("auto_threshold", 0.85)):
            item["status"], item["message"] = "ready", None
        else:
            item["status"], item["message"] = "review", "Unsicherer Treffer, bitte prüfen"
    except tmdb.TMDBError as e:
        item.update(match=None, target=None, confidence=0, status="error", message=str(e))
    return item


def scan(force=False):
    """Scannt die Eingangsordner. Bereits bekannte, unveränderte Dateien werden nicht neu abgefragt."""
    settings = config.get()
    known = db.all_item_ids()
    seen = set()
    new_count = 0
    with scan_lock:
        state["scanning"] = True
        try:
            for path, st in iter_videos(settings):
                iid = item_id(path)
                seen.add(iid)
                k = known.get(iid)
                if k and not force:
                    status, size, mtime = k
                    if status in ("done", "ignored") or (size == st.st_size and status != "error"):
                        continue
                item = evaluate(path, st, settings)
                db.upsert_item(item)
                new_count += 1
            # Verschwundene, offene Einträge entfernen
            for iid, (status, _, _) in known.items():
                if iid not in seen and status not in ("done",):
                    it = db.get_item(iid)
                    if it and not os.path.exists(it["path"]):
                        db.delete_item(iid)
        finally:
            state["scanning"] = False
    return new_count


def rematch(iid, media_type, tmdb_id, season=None, episode=None):
    it = db.get_item(iid)
    if not it:
        raise KeyError(iid)
    settings = config.get()
    parsed = it["parsed"]
    if media_type == "tv":
        parsed["kind"] = "episode"
        if season is not None and episode is not None:
            parsed["season"], parsed["episode"], parsed["absolute"] = int(season), int(episode), None
    else:
        parsed["kind"] = "movie"
    cand = tmdb.movie(tmdb_id) if media_type == "movie" else tmdb.tv(tmdb_id)
    if not cand:
        raise ValueError("TMDB-Eintrag nicht gefunden")
    info = matcher.resolve(parsed, cand)
    old_cands = (it.get("match") or {}).get("candidates") or []
    st = os.stat(it["path"])
    item = evaluate(it["path"], st, settings, parsed=parsed, info=info, candidates=old_cands)
    if item["target"] and not info.get("error"):
        item["status"], item["message"], item["confidence"] = "ready", "Manuell zugeordnet", 1.0
    db.upsert_item(item)
    return db.get_item(iid)


def apply_items(ids):
    settings = config.get()
    results, done_any = [], False
    for iid in ids:
        it = db.get_item(iid)
        if not it:
            continue
        try:
            ok, msg, hid = renamer.apply(it, settings, parser.companions(it["path"]))
        except OSError as e:
            ok, msg, hid = False, f"Fehler: {e.strerror or e}", None
        db.set_status(iid, "done" if ok else "error", msg)
        results.append({"id": iid, "ok": ok, "message": msg, "history": hid, "target": it["target"]})
        done_any = done_any or (ok and hid)
    if done_any:
        notify([r for r in results if r["ok"] and r["history"]])
    return results


def notify(results):
    s = config.get()
    if s.get("plex_url") and s.get("plex_token"):
        try:
            url = s["plex_url"].rstrip("/") + "/library/sections/all/refresh?" + urllib.parse.urlencode(
                {"X-Plex-Token": s["plex_token"]}
            )
            urllib.request.urlopen(urllib.request.Request(url), timeout=10).close()
        except Exception as e:  # noqa: BLE001
            log.warning("Plex-Refresh fehlgeschlagen: %s", e)
    if s.get("webhook_url"):
        try:
            body = json.dumps(
                {"count": len(results), "files": [os.path.basename(r["target"] or "") for r in results]}
            ).encode()
            req = urllib.request.Request(
                s["webhook_url"], data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # noqa: BLE001
            log.warning("Webhook fehlgeschlagen: %s", e)


def watch_loop(stop_event):
    """Watch-Ordner: neue Dateien erkennen, sobald sie fertig geschrieben sind, sichere Treffer automatisch einsortieren."""
    sizes = {}
    last_purge = 0
    while not stop_event.is_set():
        if time.time() - last_purge > 3600:
            last_purge = time.time()
            try:
                files.purge_expired()
            except Exception:  # noqa: BLE001
                log.exception("Papierkorb leeren fehlgeschlagen")
        s = config.get()
        interval = max(15, int(s.get("watch_interval") or 60))
        if s.get("watch_enabled") and s.get("tmdb_api_key"):
            state["watch_running"] = True
            try:
                settle = int(s.get("watch_settle_seconds", 120) if s.get("watch_settle_seconds") is not None else 120)
                now = time.time()
                stable = set()
                current = {}
                for path, st in iter_videos(s):
                    current[path] = st.st_size
                    if sizes.get(path) == st.st_size and now - st.st_mtime > settle:
                        stable.add(item_id(path))
                sizes = current
                if stable:
                    scan()
                    ready = [i["id"] for i in db.list_items() if i["status"] == "ready" and i["id"] in stable]
                    if ready:
                        res = apply_items(ready)
                        log.info("Automatisch einsortiert: %d", sum(1 for r in res if r["ok"]))
                state["last_error"] = None
            except Exception as e:  # noqa: BLE001
                log.exception("Watch-Fehler")
                state["last_error"] = str(e)
            state["watch_last_run"] = time.time()
        else:
            state["watch_running"] = False
        stop_event.wait(interval)
