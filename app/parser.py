"""Dateinamen-Erkennung (eigener, abhängigkeitsfreier Parser).

Erkennt Filme, Serien (S01E02, 1x02, Staffel/Folge, Mehrfachfolgen) und
Anime (absolute Nummerierung, Fansub-Gruppen in eckigen Klammern).
"""
import os
import re

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm", ".flv", ".divx", ".iso",
}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}

# Begriffe, an denen der Titel endet (Qualität, Quelle, Codec, Sprache ...)
STOP_TOKENS = {
    "2160p", "1080p", "1080i", "720p", "576p", "480p", "4k", "uhd", "hd", "fhd", "sd",
    "bluray", "blu-ray", "bdrip", "brrip", "bdremux", "remux", "web", "web-dl", "webdl", "webrip", "webhd",
    "hdtv", "dvdrip", "dvd", "dvdr", "dvd9", "dvd5", "hdrip", "tvrip", "satrip", "cam", "ts", "telesync", "vhsrip",
    "x264", "x265", "h264", "h265", "h.264", "h.265", "hevc", "avc", "xvid", "divx", "av1", "vp9", "10bit", "8bit",
    "hdr", "hdr10", "hdr10+", "dv", "dovi", "sdr", "imax",
    "aac", "ac3", "eac3", "dd", "dd5", "ddp", "ddp5", "dts", "dts-hd", "truehd", "atmos", "flac", "mp3", "dl", "ml",
    "german", "deutsch", "ger", "english", "eng", "multi", "dual", "subbed", "dubbed", "ger-dub", "gersub",
    "proper", "repack", "rerip", "internal", "limited", "extended", "uncut", "unrated", "remastered",
    "directors", "director's", "dc", "theatrical", "complete", "festival",
    "amzn", "nf", "dsnp", "atvp", "hmax", "hulu", "pcok", "rtl", "joyn", "zdf", "ard", "crunchyroll", "cr",
    # weitere Ton-/Quellen-/Szene-Kürzel
    "ac3d", "eac3d", "dtsd", "dd51", "ddp51", "dd20", "ddp20", "ac3md", "ld", "md", "line", "mic", "hdts", "hdcam",
    "hdtc", "tc", "telecine", "camrip", "uhdrip", "bdmv", "webhdrip", "hdtvrip", "dvdscr", "screener", "r5",
    "vff", "vf", "vostfr", "truefrench", "french", "ita", "spa", "readnfo", "nfo", "retail", "upscaled", "sbs", "hou",
    "dl-ac3", "german-dl", "dubbed-dl", "hdr10plus", "dolby", "vision", "aac2", "aac20", "opus", "hfr", "sub", "subs",
}
# Webseiten im Dateinamen ("www.seite.org - Film ...", "[ www.seite.com ]", "seite.to_")
SITE = re.compile(r"(?:^|[\s\[\(._-])(?:www\.)?[a-z0-9-]{2,}\.(?:com|org|net|to|cc|ws|me|biz|info|tv|io|se|nu|ru|in|xyz|club|eu|de|at|ch)(?=$|[\s\]\)_]|\s*-)",
                  re.IGNORECASE)
# Sammelordner, deren Name nichts über den Film verrät
GENERIC_DIRS = re.compile(r"^(downloads?|movies?|filme?|serien|series|shows?|tv|anime|complete|fertig|extracted|_?unpack(ed)?|"
                          r"incoming|media|video|videos|data|mnt|user|staffel ?\d+|season ?\d+|s\d{1,2}|disc ?\d|cd ?\d|subs?)$",
                          re.IGNORECASE)

EPISODE_PATTERNS = [
    # S01E02, S01E02E03, S01E02-E03, S01 E02
    re.compile(r"\b[Ss](?P<s>\d{1,2})[ ._-]?[Ee](?P<e>\d{1,4})(?:(?:[ ._-]?-?[ ._-]?[Ee]|-)(?P<e2>\d{1,4}))?\b"),
    # 1x02, 1x02-03
    re.compile(r"\b(?P<s>\d{1,2})[xX](?P<e>\d{2,3})(?:-(?:\d{1,2}[xX])?(?P<e2>\d{2,3}))?\b"),
    # Staffel 1 Folge 2 / Season 1 Episode 2
    re.compile(
        r"\b(?:Staffel|Season|St)[ ._-]?(?P<s>\d{1,2})[ ._-]*(?:Folge|Episode|Ep|F|E)[ ._-]?(?P<e>\d{1,4})\b",
        re.I,
    ),
]
# Anime: "Show - 123", "Show - 07v2", "Show Ep 12"
ABSOLUTE_PATTERN = re.compile(r"(?:^|\s)(?:-|Ep\.?|Episode|Folge|E)\s?(?P<a>\d{1,4})(?:v\d)?(?=\s|$|\[|\()", re.I)
YEAR_PATTERN = re.compile(r"(?<!\d)(?P<y>19[2-9]\d|20[0-4]\d)(?!\d)")
BRACKETS = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
PARENS = re.compile(r"\(([^)]*)\)")
CRC = re.compile(r"\[[0-9A-Fa-f]{8}\]")


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def is_sample(path):
    name = os.path.basename(path).lower()
    return bool(re.search(r"(^|[ ._-])sample([ ._-]|$)", name))


def _strip_sites(text):
    """Webseiten-Werbung aus dem Namen entfernen, samt Trenner danach ("www.x.org - Film" -> "Film")."""
    text = re.sub(r"^\s*(?:www\.)?[a-z0-9-]{2,}\.[a-z]{2,6}\s*[-_.]?\s*(?=\w)", lambda m: "" if "." in m.group(0).strip(" -_") and
                  re.search(r"\.(com|org|net|to|cc|ws|me|biz|info|tv|io|se|nu|ru|in|xyz|club|eu|de|at|ch)\b", m.group(0), re.I)
                  and re.search(r"\s-\s|^www\.", m.group(0) + text[m.end():m.end() + 3], re.I) else m.group(0), text, flags=re.I)
    return SITE.sub(" ", text)


def _clean(text):
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -")


def _title_from(text):
    """Nimmt alles bis zum ersten Qualitäts-/Stop-Begriff."""
    words = text.split(" ")
    out = []
    for w in words:
        lw = w.lower().strip("()[]")
        if lw in STOP_TOKENS or re.fullmatch(r"\d{3,4}p|s\d{1,2}", lw):
            break
        # "Titel-1080p" / "dw-1080p": am Bindestrich vor dem Stop-Begriff abschneiden
        bits = lw.split("-")
        cut = next((i for i, b in enumerate(bits) if i and (b in STOP_TOKENS or re.fullmatch(r"\d{3,4}p", b))), None)
        if cut is not None:
            head = "-".join(w.split("-")[:cut]).strip()
            if head:
                out.append(head)
            break
        out.append(w)
    title = " ".join(out).strip(" -")
    # Gruppenname am Ende ("Title-GROUP") abschneiden
    title = re.sub(r"-[A-Za-z0-9]+$", "", title) if " " in title and re.search(r"\w-[A-Z0-9]{2,}$", title) else title
    return title.strip(" -")


def _junk(r):
    """Titel aus dem Dateinamen unbrauchbar? (leer, zu kurz, Kennung wie "a1b2c3d4", nur Zahlen)"""
    t = (r.get("title") or "").strip()
    if not t:
        return True
    compact = re.sub(r"[\s-]", "", t)
    if len(compact) < 3:
        return True
    if " " not in t and len(compact) >= 8 and re.search(r"\d", compact) and re.search(r"[a-z]", compact, re.I):
        return True  # verschleierter Name
    return False


def parse(path, anime_keywords=("anime",)):
    """Liefert ein dict mit kind (movie|episode), title, year, season, episode, episode_end, absolute, anime.

    Titel und Jahr kommen aus dem Dateinamen; ist der unbrauchbar (verschleiert, nur Kürzel) oder fehlt beim
    Film das Jahr, das im Release-Ordner steht, werden Titel und Jahr aus dem Ordnernamen genommen."""
    r = _parse(path, anime_keywords)
    parent = os.path.basename(os.path.dirname(path))
    if not parent or GENERIC_DIRS.match(parent.strip()):
        return r
    ext = os.path.splitext(path)[1]
    f = _parse(os.path.join(os.path.dirname(os.path.dirname(path)), parent + ext), anime_keywords)
    if not f.get("title") or _junk(f):
        return r
    if _junk(r):
        use = True
    elif r["kind"] == "movie" and not r.get("year") and f.get("year") and f["kind"] == "movie":
        # Film ohne Jahr im Dateinamen, aber im Ordner: Ordner ist meist der sauberere Release-Name
        use = _same_title(r["title"], f["title"])
    elif r["kind"] == "episode" and len(re.sub(r"\W", "", r["title"] or "")) <= 4 and len(f["title"]) > len(r["title"] or ""):
        use = True  # Kürzel wie "tlou.s01e01.mkv"
    else:
        use = False
    if use:
        r["title"], r["year"] = f["title"], f.get("year") or r.get("year")
        if r["kind"] == "movie" and f["kind"] == "episode":
            # Datei ohne Folge, Ordner mit Folge: Ordner gilt
            for k in ("kind", "season", "episode", "episode_end", "absolute"):
                r[k] = f[k]
    return r


def _same_title(a, b):
    na = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    nb = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    return bool(na) and (na in nb or nb in na)


def _parse(path, anime_keywords=("anime",)):
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    parent = os.path.basename(os.path.dirname(path))
    result = {
        "file": base,
        "ext": ext.lower(),
        "kind": "movie",
        "title": None,
        "year": None,
        "season": None,
        "episode": None,
        "episode_end": None,
        "absolute": None,
        "anime": False,
        "group": None,
    }

    parts = [p.lower() for p in path.split(os.sep)]
    if any(k and k.lower() in parts for k in anime_keywords):
        result["anime"] = True

    raw = stem
    m_group = re.match(r"^\s*\[([^\]]+)\]", raw)
    if m_group:
        result["group"] = m_group.group(1)
        result["anime"] = True
    raw = CRC.sub(" ", raw)
    raw = _strip_sites(raw)
    no_brackets = BRACKETS.sub(" ", raw)
    text = _clean(no_brackets)

    # --- Serie mit Staffel/Folge ---
    for pat in EPISODE_PATTERNS:
        m = pat.search(text)
        if m:
            result["kind"] = "episode"
            result["season"] = int(m.group("s"))
            result["episode"] = int(m.group("e"))
            e2 = m.groupdict().get("e2")
            if e2 and int(e2) > result["episode"]:
                result["episode_end"] = int(e2)
            before = text[: m.start()].strip(" -")
            if not before:
                # Dateiname enthält nur "S01E02" -> Ordnernamen verwenden (ggf. eine Ebene höher)
                folder = os.path.dirname(path)
                for _ in range(2):
                    name = _clean(BRACKETS.sub(" ", os.path.basename(folder)))
                    name = re.sub(r"\b(?:S\d{1,2}|Staffel ?\d+|Season ?\d+).*$", "", name, flags=re.I).strip(" -")
                    if name:
                        before = name
                        break
                    folder = os.path.dirname(folder)
            y = YEAR_PATTERN.search(before)
            if y and y.start() > 0:
                result["year"] = int(y.group("y"))
                before = before[: y.start()]
            before = PARENS.sub(" ", before)
            result["title"] = _title_from(_clean(before))
            return result

    # --- Anime / absolute Nummerierung ---
    m = ABSOLUTE_PATTERN.search(text)
    if m and (result["anime"] or " - " in text):
        before = text[: m.start()].strip(" -")
        yr = YEAR_PATTERN.search(text[m.end():])
        num = int(m.group("a"))
        # "Film - 1080" o. ä. vermeiden; Jahreszahlen sind keine Folgen
        if before and not YEAR_PATTERN.fullmatch(m.group("a")):
            result["kind"] = "episode"
            result["absolute"] = num
            result["anime"] = True
            result["title"] = _title_from(_clean(PARENS.sub(" ", before)))
            if yr:
                result["year"] = int(yr.group("y"))
            return result

    # --- Film ---
    years = list(YEAR_PATTERN.finditer(text))
    # Letzte Jahreszahl, vor der noch Titel steht (für Filme wie "2012 (2009)")
    year_match = None
    for y in years:
        if y.start() > 0:
            year_match = y
    if year_match:
        result["year"] = int(year_match.group("y"))
        title_part = text[: year_match.start()]
    else:
        title_part = text
    title_part = PARENS.sub(lambda mm: " " if not mm.group(1).strip() else " " + mm.group(1) + " ", title_part)
    title = _title_from(_clean(title_part.replace("(", " ").replace(")", " ")))
    if not title:
        # Fallback: Ordnername (z. B. "Film.2020.1080p/abc123.mkv")
        return _parse(os.path.join(os.path.dirname(os.path.dirname(path)), parent + ext), anime_keywords) if parent else result
    result["title"] = title
    return result


def companions(video_path):
    """Untertitel neben dem Video: gleicher Name + Suffix (z. B. .de.srt, .forced.ger.srt)."""
    folder = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    found = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return found
    for name in entries:
        full = os.path.join(folder, name)
        e = os.path.splitext(name)[1].lower()
        if e in SUB_EXT and name.startswith(stem) and os.path.isfile(full):
            suffix = name[len(stem):]  # z. B. ".de.srt"
            found.append({"path": full, "suffix": suffix})
    # Unterordner "Subs" nur, wenn genau ein Video im Ordner liegt
    if sum(1 for n in entries if is_video(n)) != 1:
        return found
    for sub in ("Subs", "subs", "Subtitles", "subtitles"):
        sd = os.path.join(folder, sub)
        if os.path.isdir(sd):
            for name in os.listdir(sd):
                e = os.path.splitext(name)[1].lower()
                if e in SUB_EXT:
                    lang = re.search(r"(?:^|[ ._-])(de|ger|deu|german|en|eng|english)(?:[ ._-]|$)", name, re.I)
                    tag = ""
                    if lang:
                        l = lang.group(1).lower()
                        tag = ".de" if l in ("de", "ger", "deu", "german") else ".en"
                    if "forced" in name.lower():
                        tag += ".forced"
                    found.append({"path": os.path.join(sd, name), "suffix": tag + e})
    return found
