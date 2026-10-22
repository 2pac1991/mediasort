"""Eingebauter Browser: ein Chromium auf dem Server, ferngesteuert aus der Mediasort-Oberfläche.

Die Seite wird als Bilderstrom (Screencast) ans Handy geschickt, Tipps und Texteingaben gehen zurück.
Das Captcha löst immer der Mensch selbst. Mediasort spielt nur den JDownloader: Anfragen an
127.0.0.1:9666 (Click'n'Load) werden abgefangen, die Links entschlüsselt und zum Senden angeboten."""
import base64
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse

from . import links

log = logging.getLogger("mediasort.browser")

CHROME = os.environ.get("CHROMIUM") or next(
    (p for p in (shutil.which("chromium"), shutil.which("chromium-browser"), "/usr/bin/chromium", "/opt/pw-browsers/chromium")
     if p and os.path.exists(p)), "")
IDLE_TAB = 600       # Fenster ohne Benutzung wird nach 10 Minuten geschlossen
IDLE_BROWSER = 300
ALLOW_LOCAL = os.environ.get("BROWSER_ALLOW_LOCAL") == "1"  # nur für Tests   # Chromium beendet sich 5 Minuten nach dem letzten Fenster
CNL_PATTERNS = [{"urlPattern": "http://127.0.0.1:9666/*"}, {"urlPattern": "http://localhost:9666/*"}]
# Läuft vor jedem Seitenskript: Click'n'Load-Formulare im selben Fenster abschicken statt in einem Popup
INIT_SCRIPT = r"""(() => {
  const cnl = f => /:9666\b|flash\/add/.test(f.action || "");
  const fix = f => { try { if (cnl(f)) f.target = "_self"; } catch (e) {} };
  const submit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () { fix(this); return submit.call(this); };
  addEventListener("submit", e => fix(e.target), true);
})();"""
# Fallback-Knopf "Links übernehmen": dieselbe Suche wie das Lesezeichen
GRAB_SCRIPT = r"""(() => {
  const r = {};
  const F = [...document.querySelectorAll("form")].filter(f => /9666|flash\/add/.test(f.action || ""));
  if (F.length) for (const n of ["crypted", "jk", "urls", "package"]) { const e = F[0].querySelector(`[name="${n}"]`); if (e && e.value) r[n] = e.value; }
  if (!r.crypted && !r.urls) {
    const a = [], s = [];
    for (const e of document.querySelectorAll("*")) {
      for (const x of e.attributes) if (/^on|^data-|^value$|^href$/.test(x.name)) a.push(x.value);
      if (e.tagName === "SCRIPT") s.push(e.textContent);
    }
    const t = a.concat(s).join(" ");
    const k = [...new Set(t.match(/\b[0-9a-fA-F]{32}\b/g) || [])].slice(0, 40);
    const c = [...new Set(t.match(/[A-Za-z0-9+\/]{24,}={0,2}/g) || [])].filter(x => x.length % 4 == 0 && !/^[0-9a-fA-F]+$/.test(x)).slice(0, 80);
    if (k.length && c.length) { r.keys = k; r.crypts = c; }
    else r.links = [...document.querySelectorAll('a[href^="http"]')].map(a => a.href);
  }
  return JSON.stringify(r);
})()"""
KEYS = {"Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, ""), "Tab": ("Tab", 9, "")}


class BrowserError(Exception):
    pass


def available():
    return bool(CHROME)


def check_url(url):
    """Nur normale Webseiten öffnen, keine Geräte im Heimnetz."""
    url = (url or "").strip()
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    host = urlparse(url).hostname
    if not host:
        raise ValueError("Ungültige Adresse")
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise ValueError(f"Adresse {host} nicht gefunden") from e
    for a in () if ALLOW_LOCAL else addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:
            raise ValueError("Adressen im Heimnetz können im eingebauten Browser nicht geöffnet werden")
    return url


class Tab:
    def __init__(self, user):
        self.user = user
        self.ctx = self.target = self.sid = None
        self.frames = set()
        self.frame = None
        self.seq = 0
        self.url = self.title = ""
        self.loading = False
        self.cnl = None          # {"urls": [...], "package": ""} sobald Click'n'Load abgefangen wurde
        self.error = ""
        self.popups = []         # blockierte Popup-Adressen
        self.width, self.height = 390, 700
        self.mobile = True
        self.used = time.time()
        self.cond = threading.Condition()

    def bump(self):
        with self.cond:
            self.seq += 1
            self.cond.notify_all()


class Browser:
    def __init__(self):
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.proc = self.ws = None
        self.tabs = {}           # Benutzer -> Tab
        self.waiting = {}        # Aufruf-ID -> [Event, Antwort]
        self.next_id = 0
        self.version = ""
        self.empty_since = time.time()
        self.datadir = None

    # ---------- Chromium starten und sprechen ----------
    def _start(self):
        import websocket  # nur nötig, wenn der Browser wirklich benutzt wird
        if not CHROME:
            raise BrowserError("Chromium ist nicht installiert")
        self.datadir = tempfile.mkdtemp(prefix="mediasort-browser-")
        args = [CHROME, "--headless=new", "--no-sandbox", "--no-zygote", "--disable-gpu", "--disable-dev-shm-usage",
                "--no-first-run", "--no-default-browser-check", "--disable-crash-reporter", "--mute-audio",
                "--disable-blink-features=AutomationControlled", "--disable-background-networking",
                # Iframes (Captchas) im selben Prozess, damit das Abfangen von Click'n'Load überall greift
                "--disable-features=IsolateOrigins,site-per-process,Translate,MediaRouter,"
                "PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults,LocalNetworkAccessChecks",
                "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                f"--user-data-dir={self.datadir}", "about:blank"]
        self.proc = subprocess.Popen(args, env=dict(os.environ, HOME=self.datadir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        url, end = None, time.time() + 30
        while time.time() < end:
            line = self.proc.stderr.readline()
            if not line:
                break
            m = re.search(r"ws://\S+", line)
            if m:
                url = m.group(0)
                break
        if not url:
            self.proc.kill()
            raise BrowserError("Chromium ließ sich nicht starten")
        # stderr weiter leeren, sonst bleibt Chromium irgendwann hängen
        threading.Thread(target=lambda p=self.proc: [None for _ in p.stderr], daemon=True).start()
        self.ws = websocket.create_connection(url, suppress_origin=True, enable_multithread=True, timeout=None)
        threading.Thread(target=self._reader, args=(self.ws,), daemon=True).start()
        self.version = self.call("Browser.getVersion").get("product", "").split("/")[-1]
        self.call("Fetch.enable", {"patterns": CNL_PATTERNS})
        self.call("Target.setDiscoverTargets", {"discover": True})
        log.info("Eingebauter Browser gestartet (Chromium %s)", self.version)

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None and self.ws is not None

    def _send(self, method, params=None, sid=None):
        with self.send_lock:
            self.next_id += 1
            mid = self.next_id
            msg = {"id": mid, "method": method, "params": params or {}}
            if sid:
                msg["sessionId"] = sid
            slot = [threading.Event(), None]
            self.waiting[mid] = slot
            self.ws.send(json.dumps(msg))
        return mid, slot

    def call(self, method, params=None, sid=None, timeout=20):
        mid, slot = self._send(method, params, sid)
        if not slot[0].wait(timeout):
            self.waiting.pop(mid, None)
            raise BrowserError(f"Browser antwortet nicht ({method})")
        res = slot[1]
        if "error" in res:
            raise BrowserError(res["error"].get("message", "Fehler"))
        return res.get("result", {})

    def fire(self, method, params=None, sid=None):
        """Aufruf ohne auf die Antwort zu warten (aus dem Lese-Thread heraus)."""
        try:
            self._send(method, params, sid)
        except Exception:  # noqa: BLE001
            pass

    def _reader(self, ws):
        while True:
            try:
                msg = json.loads(ws.recv())
            except Exception:  # noqa: BLE001
                break
            if "id" in msg:
                slot = self.waiting.pop(msg["id"], None)
                if slot:
                    slot[1] = msg
                    slot[0].set()
                continue
            try:
                self._event(msg.get("method"), msg.get("params") or {}, msg.get("sessionId"))
            except Exception:  # noqa: BLE001
                log.exception("Browser-Ereignis %s", msg.get("method"))
        log.info("Eingebauter Browser beendet")
        with self.lock:
            if self.ws is ws:
                self.ws = None
                for t in self.tabs.values():
                    t.error = "Der Browser wurde beendet"
                    t.bump()
                self.tabs.clear()
        for slot in list(self.waiting.values()):
            slot[1] = {"error": {"message": "Browser beendet"}}
            slot[0].set()

    # ---------- Ereignisse ----------
    def _tab_by_sid(self, sid):
        return next((t for t in list(self.tabs.values()) if t.sid == sid), None)

    def _tab_by_frame(self, frame_id):
        tabs = list(self.tabs.values())
        t = next((t for t in tabs if frame_id == t.target or frame_id in t.frames), None)
        return t or (tabs[0] if len(tabs) == 1 else None)

    def _event(self, method, p, sid):
        if method == "Fetch.requestPaused":
            return self._cnl_request(p)
        if method == "Page.screencastFrame":
            self.fire("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, sid)
            t = self._tab_by_sid(sid)
            if t:
                t.frame = p["data"]
                t.bump()
            return
        if method in ("Page.frameAttached", "Page.frameNavigated"):
            t = self._tab_by_sid(sid)
            if t:
                f = p.get("frame") or {}
                t.frames.add(p.get("frameId") or f.get("id"))
                if method == "Page.frameNavigated" and not f.get("parentId"):
                    t.url = f.get("url", "")
                    t.popups = []
                    t.bump()
            return
        if method in ("Page.frameStartedLoading", "Page.frameStoppedLoading"):
            t = self._tab_by_sid(sid)
            if t and p.get("frameId") == t.target:
                t.loading = method == "Page.frameStartedLoading"
                t.bump()
            return
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = p["targetInfo"]
            for t in list(self.tabs.values()):
                if info["targetId"] == t.target:
                    t.title, t.url = info.get("title", ""), info.get("url", "") or t.url
                    t.bump()
                elif info.get("type") == "page" and info.get("browserContextId") == t.ctx:
                    # Popups (meist Werbung) nicht anzeigen: Adresse merken, Fenster schließen.
                    # Click'n'Load aus dem Popup wird trotzdem abgefangen, das läuft über den ganzen Browser.
                    u = info.get("url", "")
                    if u and u != "about:blank" and not u.startswith("http://127.0.0.1:9666") and u not in t.popups:
                        t.popups = (t.popups + [u])[-3:]
                        t.bump()
                    if method == "Target.targetCreated":
                        threading.Timer(2.5, self._close_target, args=(info["targetId"],)).start()
            return

    def _close_target(self, target_id):
        try:
            self.call("Target.closeTarget", {"targetId": target_id}, timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def _cnl_request(self, p):
        """Mediasort antwortet wie ein JDownloader auf Port 9666."""
        req, rid = p["request"], p["requestId"]
        path = urlparse(req["url"]).path
        body, ctype = "", "text/plain"
        if path.endswith("/jdcheck.js"):
            # Seiten zeigen den Click'n'Load-Knopf nur, wenn ein JDownloader antwortet
            body, ctype = "jdownloader=true;\nvar version='43307';\n", "text/javascript"
        elif path.endswith("/crossdomain.xml"):
            body, ctype = ('<?xml version="1.0"?><cross-domain-policy><allow-access-from domain="*" /></cross-domain-policy>',
                           "text/xml")
        elif path.startswith("/flash/add"):
            body = "success\r\n"
            form = {k: v[0] for k, v in parse_qs(urlparse(req["url"]).query).items()}
            data = req.get("postData") or "".join(
                base64.b64decode(e.get("bytes", "")).decode("utf-8", "replace") for e in req.get("postDataEntries") or [])
            form.update({k: v[0] for k, v in parse_qs(data).items()})
            t = self._tab_by_frame(p.get("frameId"))
            if t and (form.get("crypted") or form.get("urls")):
                try:
                    urls = links.decode_cnl(form.get("crypted", ""), form.get("jk", ""), form.get("urls", ""))
                    t.cnl = {"urls": urls, "package": links._clean_package(form.get("package", "")) or links.guess_package(urls)}
                    log.info("Click'n'Load im eingebauten Browser (%s): %d Link(s)", t.user, len(urls))
                except ValueError as e:
                    t.error = str(e)
                t.bump()
        elif path == "/flash" or path == "/flash/":
            body = "JDownloader\r\n"
        self.fire("Fetch.fulfillRequest", {
            "requestId": rid, "responseCode": 200, "body": base64.b64encode(body.encode()).decode(),
            "responseHeaders": [{"name": "Content-Type", "value": ctype},
                                {"name": "Access-Control-Allow-Origin", "value": "*"}]})

    # ---------- Fenster ----------
    def _ua(self, mobile):
        v = self.version or "141.0.0.0"
        if mobile:
            return f"Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Mobile Safari/537.36"
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v} Safari/537.36"

    def _metrics(self, t, mobile):
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": t.width, "height": t.height, "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile}, t.sid)

    def open(self, user, url, width, height, mobile):
        url = check_url(url)
        with self.lock:
            if not self._alive():
                self._shutdown()
                self._start()
            t = self.tabs.get(user)
            if not t:
                t = Tab(user)
                t.ctx = self.call("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
                t.target = self.call("Target.createTarget", {"url": "about:blank", "browserContextId": t.ctx})["targetId"]
                t.sid = self.call("Target.attachToTarget", {"targetId": t.target, "flatten": True})["sessionId"]
                t.frames.add(t.target)
                self.tabs[user] = t
                self.call("Page.enable", None, t.sid)
                self.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT_SCRIPT}, t.sid)
                ua = self._ua(mobile)
                self.call("Emulation.setUserAgentOverride", {
                    "userAgent": ua, "acceptLanguage": "de-DE,de;q=0.9,en;q=0.8",
                    "platform": "Linux armv8l" if mobile else "Win32"}, t.sid)
            t.width, t.height = max(320, min(int(width), 1400)), max(400, min(int(height), 1400))
            t.mobile = bool(mobile)
            self._metrics(t, t.mobile)
            scale = 2 if t.mobile else 1
            self.call("Page.startScreencast", {"format": "jpeg", "quality": 62, "everyNthFrame": 1,
                                               "maxWidth": t.width * scale, "maxHeight": t.height * scale}, t.sid)
            t.cnl, t.error, t.popups, t.used = None, "", [], time.time()
            self.call("Page.navigate", {"url": url}, t.sid)
            t.bump()
        return url

    def _tab(self, user):
        t = self.tabs.get(user)
        if not t or not self._alive():
            raise ValueError("Der Browser ist nicht mehr offen")
        t.used = time.time()
        return t

    def poll(self, user, after, wait=8.0):
        t = self._tab(user)
        with t.cond:
            t.cond.wait_for(lambda: t.seq > after, timeout=wait)
        cnl, t.cnl = t.cnl, None
        err, t.error = t.error, ""
        return {"seq": t.seq, "frame": t.frame, "url": t.url, "title": t.title, "loading": t.loading,
                "popups": t.popups, "cnl": cnl, "error": err, "width": t.width, "height": t.height}

    def input(self, user, ev):
        t = self._tab(user)
        kind = ev.get("type")
        x = max(0.0, min(float(ev.get("x", 0)), 1.0)) * t.width
        y = max(0.0, min(float(ev.get("y", 0)), 1.0)) * t.height
        if kind == "tap":
            base = {"x": x, "y": y, "button": "left", "clickCount": 1}
            self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, t.sid)
            self.call("Input.dispatchMouseEvent", dict(base, type="mousePressed", buttons=1), t.sid)
            self.call("Input.dispatchMouseEvent", dict(base, type="mouseReleased", buttons=0), t.sid)
        elif kind == "scroll":
            self.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0,
                                                   "deltaY": max(-3000.0, min(float(ev.get("dy", 0)), 3000.0))}, t.sid)
        elif kind == "text":
            self.call("Input.insertText", {"text": str(ev.get("text", ""))[:500]}, t.sid)
        elif kind == "key" and ev.get("key") in KEYS:
            key, code, text = KEYS[ev["key"]]
            for typ in ("keyDown", "keyUp"):
                k = {"type": typ, "key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
                if typ == "keyDown" and text:
                    k["text"] = text
                self.call("Input.dispatchKeyEvent", k, t.sid)
        else:
            raise ValueError("Unbekannte Eingabe")

    def nav(self, user, action, url=""):
        t = self._tab(user)
        if action == "reload":
            self.call("Page.reload", None, t.sid)
        elif action == "back":
            h = self.call("Page.getNavigationHistory", None, t.sid)
            if h["currentIndex"] > 0:
                self.call("Page.navigateToHistoryEntry", {"entryId": h["entries"][h["currentIndex"] - 1]["id"]}, t.sid)
        elif action == "go":
            self.call("Page.navigate", {"url": check_url(url)}, t.sid)
        else:
            raise ValueError("Unbekannte Aktion")

    def resize(self, user, width, height):
        t = self._tab(user)
        t.width, t.height = max(320, min(int(width), 1400)), max(400, min(int(height), 1400))
        self._metrics(t, t.mobile)

    def grab(self, user):
        """Knopf "Links übernehmen": Click'n'Load-Daten oder Links direkt aus der Seite lesen."""
        t = self._tab(user)
        r = self.call("Runtime.evaluate", {"expression": GRAB_SCRIPT, "returnByValue": True}, t.sid)
        data = json.loads((r.get("result") or {}).get("value") or "{}")
        if data.get("crypted") or data.get("urls"):
            urls = links.decode_cnl(data.get("crypted", ""), data.get("jk", ""), data.get("urls", ""))
        elif data.get("keys"):
            urls = links.decode_candidates(data["keys"], data["crypts"])
        else:
            urls = links.extract("\n".join(data.get("links") or []))
            if not urls:
                raise ValueError("Auf der Seite wurden keine Links gefunden")
            return {"urls": urls, "package": "", "plain": True}
        return {"urls": urls, "package": links._clean_package(data.get("package", "")) or links.guess_package(urls)}

    def close(self, user):
        with self.lock:
            t = self.tabs.pop(user, None)
            if t and self._alive():
                try:
                    self.call("Target.disposeBrowserContext", {"browserContextId": t.ctx}, timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            if not self.tabs:
                self.empty_since = time.time()

    def _shutdown(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.ws = None
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None
        self.tabs.clear()
        if self.datadir:
            shutil.rmtree(self.datadir, ignore_errors=True)
            self.datadir = None

    def housekeeping(self):
        """Vergessene Fenster schließen und Chromium beenden, wenn niemand ihn braucht (spart Speicher)."""
        now = time.time()
        for user, t in list(self.tabs.items()):
            if now - t.used > IDLE_TAB:
                log.info("Eingebauter Browser von %s geschlossen (unbenutzt)", user)
                self.close(user)
        with self.lock:
            if self.proc and not self.tabs and now - self.empty_since > IDLE_BROWSER:
                self._shutdown()


BROWSER = Browser()


def housekeeping_loop(stop):
    while not stop.wait(30):
        try:
            BROWSER.housekeeping()
        except Exception:  # noqa: BLE001
            log.exception("Aufräumen des Browsers")
