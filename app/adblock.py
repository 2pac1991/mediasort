"""Werbeblocker für den eingebauten Browser.

Chromium fragt für jede Adresse eine Proxy-Regel (PAC) bei Mediasort ab. Werbe-Domains bekommen einen toten
Proxy und laden nie, alles andere geht direkt. Die Liste besteht aus einer eingebauten Grundliste und
öffentlichen Werbelisten, die einmal pro Woche neu geladen werden."""
import json
import logging
import os
import re
import threading
import time
import urllib.request

from . import config

log = logging.getLogger("mediasort.adblock")

CACHE = os.path.join(config.CONFIG_DIR, "adblock-hosts.txt")
MAX_AGE = 7 * 86400
SOURCES = [
    "https://pgl.yoyo.org/adservers/serverlist.php?hostformat=nohtml&showintro=0&mimetype=plaintext",
    "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
]
# Popup-, Popunder- und Bannernetzwerke, wie sie auf Linkschutz-Seiten üblich sind
BUILTIN = """
doubleclick.net googlesyndication.com googleadservices.com adservice.google.com adnxs.com amazon-adsystem.com
popads.net popcash.net propellerads.com propellerclick.com onclickads.net onclkds.com onclickperformance.com
exoclick.com exosrv.com magsrv.com realsrv.com juicyads.com trafficjunky.net trafficstars.com tsyndicate.com
adsterra.com adsterra.net a-ads.com hilltopads.net hilltopads.com clickadu.com monetag.com adcash.com
admaven.com ad-maven.com mgid.com taboola.com outbrain.com revcontent.com galaksion.com richads.com
pushground.com evadav.com rollerads.com bidgear.com adskeeper.com zeropark.com clickaine.com
highperformanceformat.com highperformancedisplayformat.com profitablecpmrate.com profitablegatecpm.com
effectivecpmgate.com effectiveratecpm.com highcpmgate.com displayvertising.com intellipopup.com popmyads.com
yllix.com bebi.com dtscout.com acint.net adx1.com ad-delivery.net adform.net criteo.com criteo.net
pubmatic.com rubiconproject.com openx.net smartadserver.com yieldmo.com zedo.com adblade.com
popunder.net popunderjs.com pop.business ads.yahoo.com adtng.com ero-advertising.com plugrush.com
trafficfactory.biz tafficfactory.com clickadilla.com adspyglass.com pushwhy.com notix.io push-sdk.com
pushprofit.net pushnest.com mondiad.net ad.plus adplus.com 5lzmaj.com
"""
# Captchas, Linkschutz und die Lesezeichen-Seiten selbst nie blockieren
KEEP = ("filecrypt.cc", "filecrypt.co", "filecrypt.to", "google.com", "gstatic.com", "googleapis.com",
        "recaptcha.net", "hcaptcha.com", "cloudflare.com", "cutcaptcha.net", "cutcaptcha.com", "jsdelivr.net",
        "filmfans.org", "serienfans.org", "keeplinks.org", "linkcrypt.ws", "hide.cx")
DEAD_PROXY = "PROXY 127.0.0.1:9"

_lock = threading.Lock()
_pac = None
_count = 0
_loading = False


def _parse(text):
    hosts = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if not line:
            continue
        parts = line.split()
        # hosts-Format ("0.0.0.0 domain") oder einfache Liste (eine oder mehrere Domains pro Zeile)
        names = parts[1:2] if re.match(r"^(0\.0\.0\.0|127\.0\.0\.1)$", parts[0]) else parts
        for h in names:
            if "." in h and re.fullmatch(r"[a-z0-9.\-_]+", h) and h not in ("localhost", "0.0.0.0"):
                hosts.add(h)
    return hosts


def _kept(h):
    return any(h == k or h.endswith("." + k) or k.endswith("." + h) for k in KEEP)


def _build(extra):
    global _pac, _count
    hosts = {h for h in _parse(BUILTIN) | extra if not _kept(h)}
    table = json.dumps(dict.fromkeys(sorted(hosts), 1), separators=(",", ":"))
    pac = ("var B=" + table + ";\nfunction FindProxyForURL(url,host){host=host.toLowerCase();var h=host;"
           "while(true){if(B.hasOwnProperty(h))return '" + DEAD_PROXY + "';var i=h.indexOf('.');"
           "if(i<0)return 'DIRECT';h=h.substring(i+1);}}\n")
    with _lock:
        _pac, _count = pac.encode(), len(hosts)


def _download():
    global _loading
    hosts = set()
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mediasort"})
            with urllib.request.urlopen(req, timeout=30) as r:
                hosts |= _parse(r.read(20_000_000).decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            log.warning("Werbeliste %s nicht geladen: %s", url, e)
    if hosts:
        try:
            tmp = CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(hosts)))
            os.replace(tmp, CACHE)
        except OSError as e:
            log.warning("Werbeliste nicht gespeichert: %s", e)
        _build(hosts)
        log.info("Werbeblocker: %d Domains", _count)
    _loading = False


def _cached():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return _parse(f.read())
    except OSError:
        return set()


def refresh():
    """Liste laden (aus dem Zwischenspeicher) und im Hintergrund erneuern, wenn sie älter als eine Woche ist."""
    global _loading
    if _pac is None:
        _build(_cached())
    try:
        stale = time.time() - os.path.getmtime(CACHE) > MAX_AGE
    except OSError:
        stale = True
    if stale and not _loading:
        _loading = True
        threading.Thread(target=_download, daemon=True).start()


def pac():
    if _pac is None:
        _build(_cached())
    return _pac
