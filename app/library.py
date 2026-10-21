"""Abgleich mit der Mediathek: Ist eine Folge oder ein Film schon vorhanden?

Die Mediathek wird einmal eingelesen (alle paar Minuten neu) und nach Titel + Staffel/Folge bzw.
Titel + Jahr indiziert. Release-Namen tragen oft den englischen Titel, die Mediathek den deutschen
("Dark Matter" vs. "Dark Matter – Der Zeitenläufer"); deshalb zählt auch ein Treffer am Wortanfang,
aber nur bei mindestens zwei Wörtern, damit "Dark" nicht zu "Dark Matter" passt."""
import os
import re
import threading
import time

from . import config, matcher, parser

CACHE_SECONDS = 300
_lock = threading.Lock()
_cache = {"ts": 0, "key": None, "episodes": {}, "movies": {}}


def _show_names(path, root):
    """Titel aus Dateiname und Serienordner ("Dark (2017) {tmdb-70523}") als normalisierte Varianten."""
    names = set()
    rel = os.path.relpath(path, root).split(os.sep)
    if len(rel) > 1:
        folder = re.sub(r"\{[^}]*\}|\[[^\]]*\]|\(\d{4}\)", " ", rel[0])
        names.add(matcher.norm(folder))
    return {n for n in names if n}


def _build():
    s = config.get()
    episodes, movies = {}, {}
    for key in ("series_root", "anime_root", "movie_root"):
        root = s.get(key)
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if not parser.is_video(fn):
                    continue
                full = os.path.join(dirpath, fn)
                p = parser.parse(full, ())
                titles = _show_names(full, root) | {matcher.norm(p.get("title") or "")}
                titles.discard("")
                if p.get("season") is not None and p.get("episode") is not None:
                    last = p.get("episode_end") or p["episode"]
                    for ep in range(p["episode"], last + 1):
                        episodes.setdefault((p["season"], ep), set()).update(titles)
                elif key == "movie_root":
                    movies.setdefault(p.get("year"), set()).update(titles)
    return episodes, movies


def _index():
    s = config.get()
    key = (s.get("series_root"), s.get("anime_root"), s.get("movie_root"))
    with _lock:
        if _cache["key"] != key or time.time() - _cache["ts"] > CACHE_SECONDS:
            _cache["episodes"], _cache["movies"] = _build()
            _cache.update(ts=time.time(), key=key)
        return _cache["episodes"], _cache["movies"]


def refresh():
    with _lock:
        _cache["ts"] = 0


def _title_match(name, library_titles):
    if not name:
        return False
    if name in library_titles:
        return True
    if len(name.split()) < 2:
        return False
    return any(t.startswith(name + " ") or name.startswith(t + " ") for t in library_titles if len(t.split()) >= 2)


def exists(filename):
    """Liefert eine kurze Beschreibung, wenn die Datei schon in der Mediathek ist, sonst None."""
    p = parser.parse(filename, ())
    name = matcher.norm(p.get("title") or "")
    episodes, movies = _index()
    if p.get("season") is not None and p.get("episode") is not None:
        last = p.get("episode_end") or p["episode"]
        eps = range(p["episode"], last + 1)
        if all(_title_match(name, episodes.get((p["season"], e), set())) for e in eps):
            return f"S{p['season']:02}E{p['episode']:02} ist schon vorhanden"
        return None
    if p.get("kind") == "movie" and p.get("year"):
        if _title_match(name, movies.get(p["year"], set())):
            return f"Film ({p['year']}) ist schon vorhanden"
    return None
