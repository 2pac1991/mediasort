"""TMDB-Client (kostenloser API-Key von themoviedb.org) mit SQLite-Cache."""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config, db

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w185"
CACHE_SEARCH = 3 * 86400
CACHE_DETAILS = 7 * 86400


class TMDBError(Exception):
    pass


def _get(path, params=None, max_age=CACHE_SEARCH):
    s = config.get()
    key = (s.get("tmdb_api_key") or "").strip()
    if not key:
        raise TMDBError("Kein TMDB-API-Key hinterlegt (Einstellungen).")
    params = dict(params or {})
    params.setdefault("language", s.get("language") or "de-DE")
    headers = {"Accept": "application/json", "User-Agent": "Saugmeister/1.0"}
    # v4-Token (lang, mit Punkten) als Bearer, sonst v3-Key als Parameter
    if len(key) > 40:
        headers["Authorization"] = "Bearer " + key
    else:
        params["api_key"] = key
    query = urllib.parse.urlencode(sorted(params.items()))
    cache_key = path + "?" + "&".join(p for p in query.split("&") if not p.startswith("api_key="))
    cached = db.cache_get(cache_key, max_age)
    if cached is not None:
        return cached
    req = urllib.request.Request(f"{API}{path}?{query}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise TMDBError("TMDB-API-Key ungültig.") from e
        if e.code == 404:
            return None
        raise TMDBError(f"TMDB-Fehler {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise TMDBError(f"TMDB nicht erreichbar: {e}") from e
    db.cache_set(cache_key, data)
    return data


def poster(path):
    return IMG + path if path else None


def search_movie(query, year=None, language=None):
    params = {"query": query, "include_adult": "false"}
    if language:
        params["language"] = language
    if year:
        params["year"] = year
    data = _get("/search/movie", params) or {}
    out = []
    for r in data.get("results", []):
        date = r.get("release_date") or ""
        out.append(
            {
                "type": "movie",
                "id": r["id"],
                "title": r.get("title") or r.get("original_title"),
                "original_title": r.get("original_title"),
                "year": int(date[:4]) if date[:4].isdigit() else None,
                "poster": poster(r.get("poster_path")),
                "popularity": r.get("popularity") or 0,
                "overview": (r.get("overview") or "")[:300],
                "genres": r.get("genre_ids") or [],
                "lang": r.get("original_language"),
            }
        )
    return out


def search_tv(query, year=None, language=None):
    params = {"query": query, "include_adult": "false"}
    if language:
        params["language"] = language
    if year:
        params["first_air_date_year"] = year
    data = _get("/search/tv", params) or {}
    out = []
    for r in data.get("results", []):
        date = r.get("first_air_date") or ""
        out.append(
            {
                "type": "tv",
                "id": r["id"],
                "title": r.get("name") or r.get("original_name"),
                "original_title": r.get("original_name"),
                "year": int(date[:4]) if date[:4].isdigit() else None,
                "poster": poster(r.get("poster_path")),
                "popularity": r.get("popularity") or 0,
                "overview": (r.get("overview") or "")[:300],
                "genres": r.get("genre_ids") or [],
                "lang": r.get("original_language"),
            }
        )
    return out


def movie(movie_id):
    r = _get(f"/movie/{movie_id}", max_age=CACHE_DETAILS)
    if not r:
        return None
    date = r.get("release_date") or ""
    return {
        "type": "movie",
        "id": r["id"],
        "title": r.get("title") or r.get("original_title"),
        "original_title": r.get("original_title"),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster": poster(r.get("poster_path")),
        "overview": (r.get("overview") or "")[:300],
    }


def tv(tv_id):
    r = _get(f"/tv/{tv_id}", max_age=CACHE_DETAILS)
    if not r:
        return None
    date = r.get("first_air_date") or ""
    return {
        "type": "tv",
        "id": r["id"],
        "title": r.get("name") or r.get("original_name"),
        "original_title": r.get("original_name"),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster": poster(r.get("poster_path")),
        "overview": (r.get("overview") or "")[:300],
        "seasons": [
            {"season": s.get("season_number"), "episodes": s.get("episode_count") or 0}
            for s in r.get("seasons", [])
        ],
        "genres": [g.get("id") for g in r.get("genres", [])],
        "lang": r.get("original_language"),
    }


def season(tv_id, season_number):
    r = _get(f"/tv/{tv_id}/season/{season_number}", max_age=CACHE_DETAILS)
    if not r:
        return {}
    return {e.get("episode_number"): e.get("name") or "" for e in r.get("episodes", [])}


def episode_title(tv_id, season_number, episode_number):
    titles = season(tv_id, season_number)
    name = titles.get(episode_number) or ""
    # TMDB liefert ohne deutsche Übersetzung oft "Episode 5" / "Folge 5"
    if not name or name.lower() in (f"episode {episode_number}", f"folge {episode_number}"):
        try:
            en = _get(f"/tv/{tv_id}/season/{season_number}", {"language": "en-US"}, max_age=CACHE_DETAILS) or {}
            for e in en.get("episodes", []):
                if e.get("episode_number") == episode_number and e.get("name"):
                    return e["name"]
        except TMDBError:
            pass
    return name


def absolute_to_season(tv_id, absolute):
    """Rechnet eine absolute Folgennummer (Anime) in Staffel/Folge um."""
    info = tv(tv_id)
    if not info:
        return None
    seasons = sorted((s for s in info["seasons"] if s["season"] and s["season"] > 0), key=lambda s: s["season"])
    remaining = absolute
    for s in seasons:
        if remaining <= s["episodes"]:
            return s["season"], remaining
        remaining -= s["episodes"]
    # Manche Anime zählen pro Staffel weiter (z. B. One Piece S22 beginnt bei 1086)
    for s in seasons:
        titles = season(tv_id, s["season"])
        if absolute in titles:
            return s["season"], absolute
    return None


def test():
    _get("/configuration", max_age=0)
    return True
