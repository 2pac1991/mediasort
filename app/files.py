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
    return trash_path(full, user)


def trash_path(full, user):
    """Verschiebt eine Datei oder einen Ordner in den Papierkorb und merkt sich den Ursprung."""
    s = config.get()
    is_dir = os.path.isdir(full)
    size = _size(full)
    stored = os.path.join(s["trash_dir"], f"{time.time_ns()}", os.path.basename(full))
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


INCOMPLETE = (".part", ".!qb", ".crdownload", ".tmp", ".mediasort-part", ".partial")


def _package_dir(path, input_dirs):
    """Der Paketordner direkt unterhalb eines Download-Ordners, in dem path liegt (oder None)."""
    full = os.path.abspath(path)
    for root in input_dirs or []:
        root = os.path.abspath(root)
        rel = os.path.relpath(full, root)
        if rel.startswith("..") or rel == ".":
            continue
        top = rel.split(os.sep)[0]
        if os.sep in rel and not top.startswith("."):
            return os.path.join(root, top)
    return None


def _is_finished_leftover(folder, min_size):
    """True, wenn im Ordner kein Video mehr liegt und nichts mehr geladen wird (nur noch Reste)."""
    for dp, dirnames, fns in os.walk(folder):
        for fn in fns:
            low = fn.lower()
            if low.endswith(INCOMPLETE):
                return False
            # Jedes Video außer Samples schützt den Ordner, auch kleine (sonst wären kurze Folgen weg)
            if os.path.splitext(low)[1] in VIDEO_EXT and "sample" not in low and "sample" not in dp.lower():
                return False
    return True


def _newest_mtime(folder):
    newest = os.path.getmtime(folder)
    for dp, _, fns in os.walk(folder):
        for fn in fns:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dp, fn)))
            except OSError:
                pass
    return newest


def cleanup_package(path, settings):
    """Nach dem Einsortieren: den Paketordner der Datei samt Resten (nfo, txt, rar ...) in den Papierkorb,
    sobald dort kein weiteres Video mehr liegt und nichts mehr lädt. Gibt den Ordner zurück, wenn aufgeräumt."""
    if not settings.get("cleanup_downloads", True):
        return None
    folder = _package_dir(path, settings.get("input_dirs"))
    if not folder or not os.path.isdir(folder):
        return None
    if not _is_finished_leftover(folder, int(settings.get("min_size_mb") or 0) * 1024 * 1024):
        return None
    _discard(folder, settings)
    return folder


def _discard(folder, settings):
    """Reste-Ordner endgültig löschen oder, falls so eingestellt, in den Papierkorb legen."""
    if settings.get("cleanup_permanent", True):
        shutil.rmtree(folder, ignore_errors=True)
    else:
        trash_path(folder, "Saugmeister")


def cleanup_stale(settings, min_age=1800):
    """Regelmäßig: alte Reste-Ordner im Download-Ordner aufräumen (ohne Video, seit min_age unverändert)."""
    if not settings.get("cleanup_downloads", True):
        return []
    min_size = int(settings.get("min_size_mb") or 0) * 1024 * 1024
    done = []
    for root in settings.get("input_dirs") or []:
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                if time.time() - _newest_mtime(entry.path) < min_age:
                    continue
                if _is_finished_leftover(entry.path, min_size):
                    _discard(entry.path, settings)
                    done.append(entry.name)
            except OSError:
                continue
    return done
