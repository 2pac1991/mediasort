"""Speicher-Übersicht: wie voll ist der Server, was belegt den Platz, wie schnell wächst die Mediathek.

Das Einlesen läuft im Hintergrund (bei großen Mediatheken dauert ein Durchgang ein paar Sekunden bis
Minuten) und wird zwischengespeichert. Hardlinks werden nur einmal gezählt: Was schon in der Mediathek
liegt, belegt im Download-Ordner keinen zusätzlichen Platz und wird dort extra ausgewiesen."""
import logging
import os
import shutil
import threading
import time

from . import config, files

log = logging.getLogger("mediasort")

MAX_AGE = 30 * 60          # nach 30 Minuten beim nächsten Aufruf neu einlesen
GROWTH_DAYS = 30           # Zeitraum für den Zuwachs
TOP_N = 12                 # größte Titel

_lock = threading.Lock()
_state = {"data": None, "ts": 0, "running": False, "error": None}


def _areas():
    """Bereiche in fester Reihenfolge: zuerst die Mediathek, dann Downloads und Papierkorb.
    Die Reihenfolge entscheidet, wem ein Hardlink zugerechnet wird."""
    s = config.get()
    out = []
    for key, label, kind in (("movie_root", "Filme", "movies"), ("series_root", "Serien", "series"),
                             ("anime_root", "Anime", "anime")):
        if s.get(key):
            out.append({"key": kind, "name": label, "path": s[key], "library": True})
    for k, (name, path) in files.roots().items():
        if k.startswith("dl"):
            out.append({"key": k, "name": name, "path": path, "library": False})
    if s.get("trash_dir"):
        out.append({"key": "trash", "name": "Papierkorb", "path": s["trash_dir"], "library": False})
    return out


def _disks(areas):
    """Belegung je Laufwerk/Mount (bei Unraid meist ein einziges: das Array hinter /mnt/user)."""
    seen = {}
    for a in areas:
        p = a["path"]
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        if not p:
            continue
        try:
            dev = os.stat(p).st_dev
            if dev in seen:
                seen[dev]["areas"].append(a["key"])
                continue
            du = shutil.disk_usage(p)
            seen[dev] = {"path": p, "total": du.total, "used": du.used, "free": du.free, "areas": [a["key"]]}
        except OSError:
            continue
    return list(seen.values())


def _scan():
    areas = _areas()
    seen = set()  # (dev, inode): jede Datei nur einmal zählen
    since = time.time() - GROWTH_DAYS * 86400
    titles = []
    result = []
    for a in areas:
        info = {"key": a["key"], "name": a["name"], "path": a["path"], "library": a["library"],
                "exists": os.path.isdir(a["path"]), "size": 0, "linked": 0, "files": 0, "videos": 0,
                "titles": 0, "recent": 0}
        if not info["exists"]:
            result.append(info)
            continue
        base = a["path"]
        try:
            top = [e for e in os.scandir(base) if not e.name.startswith(".")]
        except OSError:
            top = []
        info["titles"] = sum(1 for e in top if e.is_dir(follow_symlinks=False))
        per_title = {}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel = os.path.relpath(dirpath, base)
            title = None if rel == "." else rel.split(os.sep)[0]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                info["files"] += 1
                if os.path.splitext(fn)[1].lower() in files.VIDEO_EXT:
                    info["videos"] += 1
                ident = (st.st_dev, st.st_ino)
                if ident in seen:
                    # Hardlink auf etwas, das schon gezählt wurde: belegt keinen Extra-Platz
                    info["linked"] += st.st_size
                    continue
                seen.add(ident)
                info["size"] += st.st_size
                if a["library"] and st.st_mtime >= since:
                    info["recent"] += st.st_size
                if a["library"]:
                    t = title or fn
                    per_title[t] = per_title.get(t, 0) + st.st_size
        if a["library"]:
            titles.extend({"area": a["key"], "area_name": a["name"], "name": t, "size": sz}
                          for t, sz in per_title.items())
        result.append(info)

    titles.sort(key=lambda t: t["size"], reverse=True)
    disks = _disks(areas)
    library = sum(i["size"] for i in result if i["library"])
    recent = sum(i["recent"] for i in result if i["library"])
    free = sum(d["free"] for d in disks)
    per_day = recent / GROWTH_DAYS
    return {
        "areas": result,
        "disks": disks,
        "top": titles[:TOP_N],
        "library": library,
        "tracked": sum(i["size"] for i in result),
        "growth": {"days": GROWTH_DAYS, "bytes": recent,
                   "days_left": int(free / per_day) if per_day > 0 else None},
    }


def _run():
    t0 = time.time()
    try:
        data = _scan()
        data["duration"] = round(time.time() - t0, 1)
        with _lock:
            _state.update(data=data, ts=time.time(), error=None)
        log.info("Speicher eingelesen in %.1f s", time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.exception("Speicher einlesen")
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["running"] = False


def refresh():
    """Einlesen im Hintergrund starten (läuft schon eines, passiert nichts)."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
    threading.Thread(target=_run, daemon=True, name="storage-scan").start()
    return True


def get():
    """Zuletzt eingelesener Stand; ist er zu alt oder fehlt er, wird nebenbei neu eingelesen.
    Die Laufwerksbelegung (frei/belegt) ist immer aktuell, weil sie nichts kostet."""
    with _lock:
        stale = not _state["data"] or time.time() - _state["ts"] > MAX_AGE
    if stale:
        refresh()
    with _lock:
        data = dict(_state["data"]) if _state["data"] else None
        out = {"running": _state["running"], "ts": _state["ts"] or None, "error": _state["error"]}
    if data:
        data["disks"] = _disks(_areas())
    else:
        data = {"disks": _disks(_areas())}
    out.update(data)
    return out
