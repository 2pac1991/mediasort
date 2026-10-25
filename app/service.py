"""Kernlogik: Ordner scannen, zuordnen, anwenden, Watch-Ordner-Schleife."""
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db, files, library, matcher, myjd, parser, renamer, tmdb, users

log = logging.getLogger("mediasort")
scan_lock = threading.Lock()
state = {"watch_last_run": None, "watch_running": False, "scanning": False, "last_error": None, "plex": None}
INCOMPLETE = (".part", ".!qb", ".crdownload", ".tmp", ".mediasort-part", ".partial")


def item_id(path):
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


_missing_warned = set()


def iter_videos(settings):
    min_size = int(settings.get("min_size_mb") or 0) * 1024 * 1024
    for root in settings["input_dirs"]:
        if not os.path.isdir(root):
            if root not in _missing_warned:
                _missing_warned.add(root)
                log.warning("Download-Ordner %s existiert im Container nicht – Pfad in den Einstellungen prüfen", root)
            continue
        _missing_warned.discard(root)
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
                log.info("Im Eingang: %s → %s", os.path.basename(path),
                         {"ready": "bereit", "review": "bitte prüfen", "error": "Fehler"}.get(item.get("status"), item.get("status"))
                         + (f" ({item['message']})" if item.get("message") and item.get("status") != "ready" else ""))
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
        library.refresh()
        notify([r for r in results if r["ok"] and r["history"]])
    return results


def plex_refresh(s=None):
    """Plex die Bibliotheken neu einlesen lassen. Das Ergebnis merken, damit die Oberfläche Fehler zeigen kann."""
    s = s or config.get()
    if not (s.get("plex_url") and s.get("plex_token")):
        state["plex"] = {"ok": False, "ts": time.time(), "error": "Plex ist in den Einstellungen nicht eingetragen"}
        return state["plex"]
    try:
        url = s["plex_url"].rstrip("/") + "/library/sections/all/refresh?" + urllib.parse.urlencode(
            {"X-Plex-Token": s["plex_token"]}
        )
        urllib.request.urlopen(urllib.request.Request(url), timeout=10).close()
        state["plex"] = {"ok": True, "ts": time.time(), "error": None}
        log.info("Plex-Bibliotheken werden neu eingelesen")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "Connection refused" in msg or "timed out" in msg or "No route" in msg or "Name or service" in msg:
            msg = f"Plex unter {s['plex_url']} nicht erreichbar – läuft Plex, und stimmt die Adresse in den Einstellungen?"
        elif "401" in msg:
            msg = "Plex lehnt das Token ab (401) – bitte in den Einstellungen ein neues Plex-Token eintragen"
        state["plex"] = {"ok": False, "ts": time.time(), "error": msg}
        log.warning("Plex-Refresh fehlgeschlagen: %s", msg)
    return state["plex"]


def plex_sections():
    """Bibliotheken in Plex mit ihren Ordnern – zum Prüfen, ob Plex dort sucht, wo Saugmeister einsortiert."""
    import xml.etree.ElementTree as ET
    s = config.get()
    if not (s.get("plex_url") and s.get("plex_token")):
        raise ValueError("Plex-Adresse und Token fehlen in den Einstellungen")
    url = s["plex_url"].rstrip("/") + "/library/sections?" + urllib.parse.urlencode({"X-Plex-Token": s["plex_token"]})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/xml"}), timeout=10) as r:
            root = ET.fromstring(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ValueError("Plex lehnt das Token ab (401) – bitte ein neues Plex-Token eintragen") from e
        raise ValueError(f"Plex antwortet mit Fehler {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Plex nicht erreichbar unter {s['plex_url']}: {e}") from e
    out = []
    for d in root.findall("Directory"):
        out.append({"title": d.get("title"), "type": d.get("type"),
                    "paths": [l.get("path") for l in d.findall("Location")],
                    "refreshing": d.get("refreshing") == "1"})
    return out


def webhook_entry(result):
    """Lesbare Angaben zu einem einsortierten Eintrag, z. B. für eine Home-Assistant-Benachrichtigung."""
    it = db.get_item(result["id"]) or {}
    m = it.get("match") or {}
    kind = "movie" if m.get("type") == "movie" else ("anime" if (it.get("parsed") or {}).get("anime") or m.get("is_anime") else "series")
    title = m.get("title") or os.path.basename(result.get("target") or "")
    year = m.get("year")
    text = f"{title} ({year})" if year else title
    if kind != "movie" and m.get("season") is not None and m.get("episode") is not None:
        text += f" – S{m['season']:02}E{m['episode']:02}"
        if m.get("eptitle"):
            text += f" {m['eptitle']}"
    return {
        "kind": kind, "title": title, "year": year, "season": m.get("season"), "episode": m.get("episode"),
        "eptitle": m.get("eptitle"), "poster": m.get("poster"), "file": os.path.basename(result.get("target") or ""),
        "text": text,
    }


def notify(results):
    s = config.get()
    plex_refresh(s)
    # Webhook aus den Einstellungen plus die eigenen Webhooks der Nutzer (z. B. ihr Home Assistant)
    urls = list(dict.fromkeys([u for u in [s.get("webhook_url")] + users.all_webhooks() if u]))
    if urls:
        entries = [webhook_entry(r) for r in results]
        send_webhooks(urls, {
            "count": len(results),
            "files": [os.path.basename(r["target"] or "") for r in results],
            "items": entries,
            "text": "\n".join(e["text"] for e in entries),
        })


def send_webhooks(urls, payload, wait=False):
    """Schickt die Meldung an alle Adressen parallel, damit ein langsamer Empfänger das Einsortieren nicht bremst."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    errors = {}

    def post(url):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # noqa: BLE001
            errors[url] = str(e)
            log.warning("Webhook %s fehlgeschlagen: %s", url.split("/api/webhook/")[0], e)

    threads = [threading.Thread(target=post, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    if wait:
        for t in threads:
            t.join(15)
    return errors


_captchas_seen = set()


def check_captchas():
    """Neue Captchas im JDownloader: einmal pro Captcha Bescheid geben (Webhook, z. B. aufs Handy)."""
    jobs = myjd.captcha_list()
    new = [j for j in jobs if j["id"] not in _captchas_seen]
    _captchas_seen.intersection_update({j["id"] for j in jobs})
    _captchas_seen.update(j["id"] for j in jobs)
    if not new:
        return
    log.info("JDownloader wartet auf %d Captcha(s): %s", len(new), ", ".join(j["hoster"] for j in new))
    urls = list(dict.fromkeys(users.link_webhooks()))
    if not urls:
        return
    hosts = ", ".join(dict.fromkeys(j["hoster"] for j in new if j["hoster"]))
    solvable = [j for j in new if j["kind"] in ("text", "click", "multiclick")]
    text = (f"Captcha{' für ' + hosts if hosts else ''} – antippen und in Saugmeister lösen"
            if solvable else f"Captcha{' für ' + hosts if hosts else ''} – muss im Browser gelöst werden")
    send_webhooks(urls, {"type": "captcha", "count": len(new), "items": [], "files": [],
                         "title": "🔐 Captcha lösen", "text": text, "path": "/?captcha=1"})


def jd_loop(stop_event):
    """JDownloader im eigenen Thread abfragen (Captchas, schon vorhandene Dateien).
    So muss der Watch-Ordner nie warten, wenn My.JDownloader langsam oder nicht erreichbar ist."""
    last_captcha = last_dedupe = 0
    captcha_every = 20
    while not stop_event.is_set():
        if myjd.configured():
            if time.time() - last_captcha > captcha_every:
                last_captcha = time.time()
                try:
                    check_captchas()
                    captcha_every = 20
                except Exception as e:  # noqa: BLE001
                    captcha_every = 120  # JDownloader nicht erreichbar: seltener fragen
                    log.debug("Captchas nicht abrufbar: %s", e)
            if config.get().get("jd_skip_existing") and time.time() - last_dedupe > 60:
                last_dedupe = time.time()
                try:
                    removed = myjd.remove_existing()
                    if removed:
                        log.info("Schon vorhanden, aus dem JDownloader-Linksammler entfernt: %s", ", ".join(removed))
                except Exception as e:  # noqa: BLE001
                    log.debug("Abgleich mit JDownloader nicht möglich: %s", e)
        stop_event.wait(5)


def watch_loop(stop_event):
    """Watch-Ordner: neue Dateien erkennen, sobald sie fertig geschrieben sind, sichere Treffer automatisch einsortieren."""
    sizes = {}
    last_purge = 0
    last_cleanup = 0
    while not stop_event.is_set():
        if time.time() - last_cleanup > 600:
            last_cleanup = time.time()
            try:
                cleaned = files.cleanup_stale(config.get())
                if cleaned:
                    log.info("Reste im Download-Ordner aufgeräumt: %s", ", ".join(cleaned))
            except Exception:  # noqa: BLE001
                log.exception("Aufräumen des Download-Ordners fehlgeschlagen")
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
