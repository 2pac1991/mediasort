"""Links an JDownloader übergeben: Mediasort legt eine .crawljob-Datei in den Ordner, den JDownloaders
Erweiterung "Folder Watch" überwacht. JDownloader holt sie dort ab und startet den Download."""
import json
import os
import re
import time

from . import config, db

URL_RE = re.compile(r"""https?://[^\s<>"'`]+""", re.IGNORECASE)
MAX_LINKS = 500


def extract(text):
    """Alle Links aus einem eingefügten Text (auch ganze Nachrichten), ohne Doppelte, Reihenfolge bleibt."""
    urls = [u.rstrip(".,;:!?)]}") for u in URL_RE.findall(text or "")]
    return list(dict.fromkeys(u for u in urls if len(u) > 10))


def _clean_package(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", (name or "").strip())
    return re.sub(r"\s+", " ", name)[:120]


def send(text, package, user):
    watch = (config.get().get("jd_watch_dir") or "").strip()
    if not watch:
        raise ValueError("JDownloader ist noch nicht eingerichtet (Einstellungen → JDownloader)")
    if not os.path.isdir(watch):
        raise FileNotFoundError(f"Der JDownloader-Ordner {watch} existiert nicht")
    urls = extract(text)
    if not urls:
        raise ValueError("Keine Links gefunden. Links müssen mit http:// oder https:// beginnen.")
    if len(urls) > MAX_LINKS:
        raise ValueError(f"Höchstens {MAX_LINKS} Links auf einmal")
    package = _clean_package(package)
    job = {
        "text": "\n".join(urls),
        "autoStart": "TRUE",
        "autoConfirm": "TRUE",
        "enabled": "TRUE",
        "forcedStart": "DEFAULT",
        "comment": f"Von {user} über Mediasort",
    }
    if package:
        job["packageName"] = package
    name = f"mediasort-{time.time_ns()}"
    # Erst unter verstecktem Namen schreiben, dann umbenennen: JDownloader soll keine halbe Datei lesen
    tmp = os.path.join(watch, f".{name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([job], f, ensure_ascii=False)
    os.replace(tmp, os.path.join(watch, f"{name}.crawljob"))
    db.add_links(user, package, urls)
    return len(urls)
