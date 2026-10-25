"""Zielpfade nach Plex-Schema bauen und Dateien verlinken/verschieben/kopieren."""
import errno
import os
import re
import shutil

from . import db, files

TOKEN = re.compile(r"\{(\w+)(?::(\d+))?\}")
ILLEGAL = re.compile(r'[<>"|?*\x00-\x1f]')


def sanitize(part):
    part = part.replace(":", " -").replace("/", "-").replace("\\", "-")
    part = ILLEGAL.sub("", part)
    part = re.sub(r"\s+", " ", part).strip()
    return part.rstrip(". ") or "_"


def render(fmt, values):
    def rep(m):
        key, pad = m.group(1), m.group(2)
        if key not in values:
            return m.group(0)
        v = values[key]
        if v is None:
            return ""
        if pad and isinstance(v, int):
            s = str(v).zfill(int(pad))
            if key == "episode" and values.get("episode_end"):
                s += "-e" + str(values["episode_end"]).zfill(int(pad))
            return s
        return str(v)

    out = TOKEN.sub(rep, fmt)
    # Leere Teile aufräumen: "Titel () " oder " - " am Ende
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\{tmdb-\}", "", out)
    out = re.sub(r"\s+-\s*(?=/|$)", "", out)
    parts = [sanitize(p) for p in out.split("/") if p.strip()]
    return parts


def target_for(parsed, info, settings):
    """Liefert den vollständigen Zielpfad (inkl. Endung) oder None."""
    if not info:
        return None
    if info["type"] == "movie":
        root, fmt = settings["movie_root"], settings["movie_format"]
        values = {"title": info["title"], "year": info.get("year"), "tmdbid": info["id"]}
    else:
        if info.get("season") is None or info.get("episode") is None:
            return None
        anime = parsed.get("anime") or info.get("is_anime")
        root = settings["anime_root"] if anime else settings["series_root"]
        fmt = settings["anime_format"] if anime else settings["series_format"]
        values = {
            "show": info["title"], "title": info["title"], "year": info.get("year"), "tmdbid": info["id"],
            "season": info["season"], "episode": info["episode"], "episode_end": info.get("episode_end"),
            "eptitle": info.get("eptitle") or "", "absolute": parsed.get("absolute"),
        }
    parts = render(fmt, values)
    return os.path.join(root, *parts) + parsed["ext"]


# ---------- Dateioperationen ----------
def _unique(path):
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def _transfer(src, dst, action, fallback):
    """Führt eine Aktion aus und gibt die tatsächlich verwendete Aktion zurück."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if action == "hardlink":
        try:
            os.link(src, dst)
            return "hardlink"
        except OSError as e:
            if e.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            if fallback == "fail":
                raise OSError(e.errno, "Hardlink nicht möglich (anderes Laufwerk/Mount)") from e
            action = fallback
    if action in ("move", "rename"):
        try:
            os.rename(src, dst)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            shutil.move(src, dst)
        return action
    if action == "copy":
        tmp = dst + ".mediasort-part"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return "copy"
    raise ValueError(f"Unbekannte Aktion {action}")


def apply(item, settings, companions=()):
    """Verarbeitet ein Item. Gibt (ok, message, history_id) zurück."""
    src, dst = item["path"], item["target"]
    if not dst:
        return False, "Kein Ziel", None
    if not os.path.exists(src):
        return False, "Quelldatei existiert nicht mehr", None
    action = settings["action"]
    if action == "rename":
        dst = os.path.join(os.path.dirname(src), os.path.basename(dst))
    if os.path.abspath(src) == os.path.abspath(dst):
        return True, "Bereits korrekt benannt", None
    if os.path.exists(dst):
        mode = settings["on_conflict"]
        if mode == "trash_source" and action != "rename":
            # Schon in der Mediathek: doppelten Download (samt Untertiteln) in den Papierkorb
            for path in [src] + [c["path"] for c in companions]:
                if os.path.lexists(path):
                    files.trash_path(path, "Saugmeister")
            _remove_empty_dirs(src, settings.get("input_dirs") or [])
            files.cleanup_package(src, settings)
            return True, "Schon in der Mediathek, Download in den Papierkorb verschoben", None
        if os.path.samefile(src, dst):
            return True, "Ziel ist bereits verlinkt", None
        if mode == "skip":
            return False, "Ziel existiert bereits (übersprungen)", None
        if mode == "keep_both":
            dst = _unique(dst)
        elif mode == "overwrite":
            os.remove(dst)
    used = _transfer(src, dst, action, settings.get("hardlink_fallback", "copy"))
    extras = []
    if settings.get("include_subtitles", True):
        dst_stem = os.path.splitext(dst)[0]
        for c in companions:
            cdst = dst_stem + c["suffix"]
            if os.path.exists(cdst):
                continue
            try:
                cused = _transfer(c["path"], cdst, action, settings.get("hardlink_fallback", "copy"))
                extras.append({"src": c["path"], "dst": cdst, "action": cused})
            except OSError:
                pass
    if used == "move":
        # Paketordner im Download-Ordner aufräumen: leer -> löschen, nur noch Reste -> in den Papierkorb
        _remove_empty_dirs(src, settings.get("input_dirs") or [])
        files.cleanup_package(src, settings)
    parsed, info = item.get("parsed") or {}, item.get("match") or {}
    kind = "movie" if info.get("type") == "movie" else ("anime" if parsed.get("anime") or info.get("is_anime") else "series")
    hid = db.add_history(used, src, dst, kind, info.get("title"), extras)
    note = "" if used == action else f" (Fallback: {used})"
    return True, f"OK{note}", hid


def _remove_empty_dirs(path, stop_roots):
    stop = {os.path.abspath(r) for r in stop_roots if r}
    d = os.path.dirname(os.path.abspath(path))
    while d and d not in stop and d != "/":
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def undo(entry, settings):
    roots = [settings["movie_root"], settings["series_root"], settings["anime_root"], *settings["input_dirs"]]
    ops = [{"src": entry["src"], "dst": entry["dst"], "action": entry["action"]}] + list(entry.get("extras") or [])
    for op in ops:
        src, dst, action = op["src"], op["dst"], op["action"]
        if not os.path.exists(dst):
            continue
        if action in ("hardlink", "copy"):
            if action == "hardlink" and os.path.exists(src) and not os.path.samefile(src, dst):
                raise OSError("Ziel wurde inzwischen verändert, Rückgängig abgebrochen")
            if action == "copy" and not os.path.exists(src):
                raise OSError("Original fehlt, Kopie wird nicht gelöscht")
            os.remove(dst)
        else:
            if os.path.exists(src):
                raise OSError("Am Ursprungsort liegt schon eine Datei")
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dst, src)
        _remove_empty_dirs(dst, roots)
    db.mark_undone(entry["id"])
