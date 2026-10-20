"""Ordnet erkannte Dateien einem TMDB-Eintrag zu und bewertet die Sicherheit."""
import difflib
import re
import unicodedata

from . import config, tmdb

ANIMATION_GENRE = 16


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(the|der|die|das|a|an)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def score(parsed, cand):
    sim = max(similarity(parsed["title"], t) for t in (cand.get("title"), cand.get("original_title"), cand.get("en_title")))
    s = sim
    if parsed.get("year") and cand.get("year"):
        diff = abs(parsed["year"] - cand["year"])
        s += 0.1 if diff == 0 else (0.03 if diff == 1 else -0.15)
    return max(0.0, min(1.0, s))


def _queries(title):
    """Mehrere Suchvarianten: voller Titel, Teile bei ' - ', ohne Klammern."""
    qs = [title]
    if " - " in title:
        qs += [p.strip() for p in title.split(" - ") if len(p.strip()) > 1]
    stripped = re.sub(r"\(.*?\)", "", title).strip()
    if stripped and stripped not in qs:
        qs.append(stripped)
    return qs


def find_candidates(parsed):
    kind = "movie" if parsed["kind"] == "movie" else "tv"
    search = tmdb.search_movie if kind == "movie" else tmdb.search_tv
    # Dateinamen tragen oft den englischen Titel, TMDB liefert aber den deutschen:
    # zusätzlich auf Englisch suchen und den englischen Titel mitvergleichen.
    lang = config.get().get("language") or "de-DE"
    english = not lang.lower().startswith("en")
    seen, cands = set(), []
    for q in _queries(parsed["title"] or ""):
        results = search(q, parsed.get("year"))
        if not results and parsed.get("year"):
            results = search(q)
        if english:
            en = search(q, parsed.get("year"), language="en-US")
            if not en and parsed.get("year"):
                en = search(q, language="en-US")
            en_titles = {r["id"]: r["title"] for r in en}
            for c in results:
                c["en_title"] = en_titles.get(c["id"])
        for c in results[:10]:
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            c["score"] = score(parsed, c)
            # Bei Varianten (Teil-Titel) leicht abwerten
            if q != parsed["title"]:
                c["score"] *= 0.95
            cands.append(c)
    # Beliebtheit als Tiebreaker
    cands.sort(key=lambda c: (round(c["score"], 2), c.get("popularity", 0)), reverse=True)
    return cands


def resolve(parsed, cand):
    """Ergänzt Folgen-Infos (Staffel, Folge, Folgentitel) für einen gewählten Kandidaten."""
    info = {"type": cand["type"], "id": cand["id"], "title": cand["title"], "year": cand.get("year"),
            "poster": cand.get("poster"), "overview": cand.get("overview")}
    if cand["type"] == "movie":
        return info
    details = tmdb.tv(cand["id"]) or {}
    info["year"] = details.get("year") or info["year"]
    info["is_anime"] = ANIMATION_GENRE in (details.get("genres") or cand.get("genres") or []) and (
        details.get("lang") or cand.get("lang")
    ) == "ja"
    season, episode = parsed.get("season"), parsed.get("episode")
    if season is None and parsed.get("absolute"):
        mapped = tmdb.absolute_to_season(cand["id"], parsed["absolute"])
        if mapped:
            season, episode = mapped
        else:
            info["error"] = f"Folge {parsed['absolute']} nicht in TMDB gefunden"
            return info
    info["season"] = season
    info["episode"] = episode
    info["episode_end"] = parsed.get("episode_end")
    if season is not None and episode is not None:
        t1 = tmdb.episode_title(cand["id"], season, episode)
        if parsed.get("episode_end"):
            t2 = tmdb.episode_title(cand["id"], season, parsed["episode_end"])
            # Doppelfolgen "Titel (1)" + "Titel (2)" -> "Titel"
            base1 = re.sub(r"\s*\(\d\)$", "", t1 or "")
            base2 = re.sub(r"\s*\(\d\)$", "", t2 or "")
            info["eptitle"] = base1 if base1 == base2 else " & ".join(t for t in (t1, t2) if t)
        else:
            info["eptitle"] = t1
        if not info.get("eptitle"):
            known = tmdb.season(cand["id"], season)
            if known and episode not in known:
                info["error"] = f"S{season:02}E{episode:02} existiert laut TMDB nicht"
    return info


def match(parsed):
    """Liefert (match_info|None, confidence, candidates)."""
    if not parsed.get("title"):
        return None, 0.0, []
    cands = find_candidates(parsed)
    if not cands:
        return None, 0.0, []
    best = cands[0]
    conf = best["score"]
    # Eindeutigkeit: knapper Abstand zum Zweitplatzierten senkt die Sicherheit
    if len(cands) > 1 and cands[1]["score"] > conf - 0.05 and cands[1]["id"] != best["id"]:
        conf -= 0.1
    info = resolve(parsed, best)
    if info.get("error"):
        conf = min(conf, 0.5)
    return info, round(max(0.0, conf), 3), cands[:8]
