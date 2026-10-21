"""Dateiverwaltung für Downloads und Mediathek, mit Papierkorb statt endgültigem Löschen."""
import os
import shutil
import time

from . import config, db

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm", ".iso"}


def roots():
    """Die Bereiche, die in der Oberfläche auftauchen: key -> (Name, Pfad)."""
    s = config.get()
    out = {}
    dirs = s.get("input_dirs") or []
    for i, d in enumerate(dirs):
        out[f"dl{i}"] = ("Downloads" if len(dirs) == 1 else f"Downloads {i + 1}", d)
    out["movies"] = ("Filme", s.get("movie_root"))
    out["series"] = ("Serien", s.get("series_root"))
    out["anime"] = ("Anime", s.get("anime_root"))
    return {k: v for k, v in out.items() if v[1]}


def resolve(root_key, rel=""):
    """Pfad innerhalb eines Bereichs auflösen; alles außerhalb wird abgelehnt."""
    r = roots().get(root_key)
    if not r:
        raise ValueError("Unbekannter Bereich")
    base = os.path.realpath(r[1])
    full = os.path.realpath(os.path.join(base, (rel or "").lstrip("/")))
    if full != base and not full.startswith(base + os.sep):
        raise PermissionError("Pfad liegt außerhalb des Bereichs")
    return base, full


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def list_dir(root_key, rel=""):
    base, full = resolve(root_key, rel)
    if not os.path.isdir(full):
        raise FileNotFoundError("Ordner existiert nicht")
    entries = []
    with os.scandir(full) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                st = e.stat()
                is_dir = e.is_dir()
                count = None
                if is_dir:
                    try:
                        count = sum(1 for n in os.listdir(e.path) if not n.startswith("."))
                    except OSError:
                        count = 0
                entries.append({
                    "name": e.name, "dir": is_dir, "size": None if is_dir else st.st_size, "count": count,
                    "mtime": st.st_mtime, "video": (not is_dir) and os.path.splitext(e.name)[1].lower() in VIDEO_EXT,
                })
            except OSError:
                continue
    rel_clean = os.path.relpath(full, base)
    return {"root": root_key, "path": "" if rel_clean == "." else rel_clean, "entries": entries}


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(src, dst)


def delete(root_key, rel, user):
    base, full = resolve(root_key, rel)
    if full == base:
        raise PermissionError("Der Hauptordner kann nicht gelöscht werden")
    if not os.path.lexists(full):
        raise FileNotFoundError("Existiert nicht mehr")
    s = config.get()
    is_dir = os.path.isdir(full)
    size = _size(full)
    stored = os.path.join(s["trash_dir"], f"{int(time.time() * 1000)}", os.path.basename(full))
    _move(full, stored)
    return db.add_trash(user, full, stored, is_dir, size)


def rename(root_key, rel, new_name, user=None):
    new_name = (new_name or "").strip()
    if not new_name or new_name in (".", "..") or "/" in new_name or "\0" in new_name or new_name.startswith("."):
        raise ValueError("Ungültiger Name")
    base, full = resolve(root_key, rel)
    if full == base:
        raise PermissionError("Der Hauptordner kann nicht umbenannt werden")
    dst = os.path.join(os.path.dirname(full), new_name)
    if os.path.lexists(dst):
        raise FileExistsError("Diesen Namen gibt es in dem Ordner schon")
    os.rename(full, dst)
    return os.path.basename(dst)


def restore(tid):
    t = db.get_trash(tid)
    if not t:
        raise KeyError(tid)
    if os.path.lexists(t["orig"]):
        raise FileExistsError("Am ursprünglichen Ort liegt schon etwas mit diesem Namen")
    if not os.path.lexists(t["stored"]):
        db.delete_trash(tid)
        raise FileNotFoundError("Nicht mehr im Papierkorb")
    _move(t["stored"], t["orig"])
    _cleanup_slot(t["stored"])
    db.delete_trash(tid)
    return t["orig"]


def purge(tid):
    t = db.get_trash(tid)
    if not t:
        raise KeyError(tid)
    if os.path.isdir(t["stored"]) and not os.path.islink(t["stored"]):
        shutil.rmtree(t["stored"], ignore_errors=True)
    elif os.path.lexists(t["stored"]):
        os.remove(t["stored"])
    _cleanup_slot(t["stored"])
    db.delete_trash(tid)


def _cleanup_slot(stored):
    try:
        os.rmdir(os.path.dirname(stored))
    except OSError:
        pass


def list_trash():
    out = []
    for t in db.list_trash():
        t["name"] = os.path.basename(t["orig"])
        t["exists"] = os.path.lexists(t["stored"])
        out.append(t)
    return out


def purge_expired():
    days = int(config.get().get("trash_days") or 14)
    cutoff = time.time() - days * 86400
    for t in db.list_trash():
        if t["ts"] < cutoff:
            purge(t["id"])
