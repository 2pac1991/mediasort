"""Einstellungen: liegen als JSON in /config/settings.json."""
import json
import os
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "tmdb_api_key": "",
    "language": "de-DE",
    "input_dirs": ["/data/Downloads"],
    "movie_root": "/data/Medien/Filme",
    "series_root": "/data/Medien/Serien",
    "anime_root": "/data/Medien/Anime",
    # Platzhalter: {title} {year} {tmdbid} {show} {season:2} {episode:2} {eptitle} {ext-frei}
    # Plex versteht {tmdb-12345} im Ordnernamen und ordnet dann garantiert richtig zu.
    "movie_format": "{title} ({year}) {tmdb-{tmdbid}}/{title} ({year})",
    "series_format": "{show} ({year}) {tmdb-{tmdbid}}/Season {season:2}/{show} ({year}) - s{season:2}e{episode:2} - {eptitle}",
    "anime_format": "{show} ({year}) {tmdb-{tmdbid}}/Season {season:2}/{show} ({year}) - s{season:2}e{episode:2} - {eptitle}",
    "action": "hardlink",  # hardlink | move | copy | rename
    "hardlink_fallback": "copy",  # copy | move | fail
    "on_conflict": "skip",  # skip | overwrite | keep_both
    "include_subtitles": True,
    "anime_keywords": ["anime"],
    "min_size_mb": 50,
    "watch_enabled": False,
    "watch_interval": 60,
    "watch_settle_seconds": 120,
    "auto_threshold": 0.85,
    "plex_url": "",
    "plex_token": "",
    "webhook_url": "",
    # Papierkorb für gelöschte Dateien (muss auf demselben Mount wie die Mediathek liegen)
    "trash_dir": "/data/.mediasort-papierkorb",
    "trash_days": 14,
}

_lock = threading.Lock()
_settings = None


def load():
    global _settings
    with _lock:
        data = dict(DEFAULTS)
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, encoding="utf-8") as f:
                    data.update(json.load(f))
            except (OSError, ValueError):
                pass
        else:
            # Beim ersten Start Werte aus Umgebungsvariablen übernehmen
            env_map = {
                "TMDB_API_KEY": "tmdb_api_key",
                "LANGUAGE": "language",
                "PLEX_URL": "plex_url",
                "PLEX_TOKEN": "plex_token",
            }
            for env, key in env_map.items():
                if os.environ.get(env):
                    data[key] = os.environ[env]
        _settings = data
        return dict(_settings)


def get():
    if _settings is None:
        load()
    return dict(_settings)


def save(new_values):
    global _settings
    current = get()
    for k, v in new_values.items():
        if k in DEFAULTS:
            current[k] = v
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)
    with _lock:
        _settings = current
    return dict(current)
