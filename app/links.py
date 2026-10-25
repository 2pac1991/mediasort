"""Links an JDownloader übergeben: Saugmeister legt eine .crawljob-Datei in den Ordner, den JDownloaders
Erweiterung "Folder Watch" überwacht. JDownloader holt sie dort ab und startet den Download."""
import base64
import json
import os
import re
import time
import urllib.parse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import config, db, myjd

URL_RE = re.compile(r"""https?://[^\s<>"'`]+""", re.IGNORECASE)
MAX_LINKS = 500


def extract(text):
    """Alle Links aus einem eingefügten Text (auch ganze Nachrichten), ohne Doppelte, Reihenfolge bleibt."""
    urls = [u.rstrip(".,;:!?)]}") for u in URL_RE.findall(text or "")]
    return list(dict.fromkeys(u for u in urls if len(u) > 10))


def _clean_package(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", (name or "").strip())
    return re.sub(r"\s+", " ", name)[:120]


def configured():
    return myjd.configured() or bool((config.get().get("jd_watch_dir") or "").strip())


def send(text, package, user):
    watch = (config.get().get("jd_watch_dir") or "").strip()
    if not configured():
        raise ValueError("JDownloader ist noch nicht eingerichtet (Einstellungen → JDownloader)")
    urls = extract(text)
    if not urls:
        raise ValueError("Keine Links gefunden. Links müssen mit http:// oder https:// beginnen.")
    if len(urls) > MAX_LINKS:
        raise ValueError(f"Höchstens {MAX_LINKS} Links auf einmal")
    package = _clean_package(package)
    if myjd.configured():
        try:
            myjd.add_links(urls, package)
        except myjd.MyJDError as e:
            raise ValueError(str(e)) from e
        db.add_links(user, package, urls)
        return len(urls)
    if not os.path.isdir(watch):
        raise FileNotFoundError(f"Der JDownloader-Ordner {watch} existiert nicht")
    job = {
        "text": "\n".join(urls),
        "autoStart": "TRUE",
        "autoConfirm": "TRUE",
        "enabled": "TRUE",
        "forcedStart": "DEFAULT",
        "comment": f"Von {user} über Saugmeister",
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


JK_HEX = re.compile(r"""['"]([0-9a-fA-F]{32})['"]""")


def _aes_links(key, crypted):
    try:
        data = base64.b64decode(crypted.replace(" ", "+"))
        if len(data) < 16:
            return []
        dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
        plain = (dec.update(data[: len(data) // 16 * 16]) + dec.finalize()).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return []
    return extract(plain.replace("\x00", " "))


def decode_candidates(keys, crypts):
    """Für Seiten, die ihr CNL-Formular erst per Skript bauen: das Lesezeichen schickt alle Schlüssel- und
    Daten-Kandidaten der Seite, hier wird jede Kombination probiert. Nur Ergebnisse mit echten Links zählen."""
    keys = [k for k in dict.fromkeys(keys) if re.fullmatch(r"[0-9a-fA-F]{32}", k)][:40]
    crypts = [c for c in dict.fromkeys(crypts) if len(c) >= 24][:80]
    found = []
    for c in crypts:
        for k in keys:
            got = _aes_links(bytes.fromhex(k), c)
            if got:
                found += got
                break
    found = list(dict.fromkeys(found))
    if not found:
        raise ValueError("Auf der Seite wurden keine Click'n'Load-Daten gefunden")
    return found


def decode_cnl(crypted="", jk="", urls=""):
    """Click'n'Load: die Links aus einem CNL-Knopf entschlüsseln.

    CNL2 schickt die Links AES-128-CBC-verschlüsselt (crypted, Base64). Der Schlüssel steckt in jk, einem
    kleinen JavaScript wie function f(){ return '3132...'; }. Das JavaScript wird nie ausgeführt, der
    Schlüssel wird nur herausgelesen. Die ältere Variante (urls) enthält die Links im Klartext."""
    if urls:
        return extract(urls)
    if not crypted or not jk:
        raise ValueError("Keine Click'n'Load-Daten gefunden")
    m = JK_HEX.search(jk)
    if not m:
        raise ValueError("Click'n'Load-Schlüssel nicht lesbar (diese Seite nutzt ein unbekanntes Verfahren)")
    found = _aes_links(bytes.fromhex(m.group(1)), crypted)
    if not found:
        raise ValueError("Keine Links in den Click'n'Load-Daten")
    return found


ARCHIVE_EXT = re.compile(r"(\.part\d+)?\.(rar|zip|7z|tar|gz|r\d{2}|\d{3}|mkv|mp4|avi|m4v|ts|mov|wmv|iso)$", re.IGNORECASE)


def guess_package(urls):
    """Paketname aus den Dateinamen der Links: gemeinsamer Anfang ohne Teil-Nummer und Endung.
    Film.2026.part1.rar + Film.2026.part2.rar -> Film.2026; Serie.S01E01.mkv + Serie.S01E02.mkv -> Serie.S01"""
    names = []
    for u in urls:
        last = urllib.parse.unquote(urllib.parse.urlparse(u).path.rstrip("/").rsplit("/", 1)[-1])
        if ARCHIVE_EXT.search(last):
            names.append(ARCHIVE_EXT.sub("", last))
    if not names:
        return ""
    prefix = os.path.commonprefix(names)
    if prefix != min(names, key=len):
        season = re.search(r"(?i)(.*S\d{1,2})E\d*$", prefix)
        if season:
            # Mehrere Folgen einer Staffel ("Serie.S01E0"): Staffel behalten -> "Serie.S01"
            prefix = season.group(1)
        else:
            # Mitten in einem Wort abgeschnitten: bis zum letzten Trennzeichen kürzen
            cut = max(prefix.rfind(c) for c in "._- ")
            prefix = prefix[:cut] if cut > 0 else prefix
    prefix = prefix.strip("._- ")
    return _clean_package(prefix) if len(prefix) >= 4 else ""
