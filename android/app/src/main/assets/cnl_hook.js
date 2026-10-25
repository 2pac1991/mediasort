// Saugmeister: fängt Click'n'Load in der Seite ab (fetch, XHR, Formulare, Beacons an 127.0.0.1:9666)
// und gibt die Daten an die App weiter. Die Seite bekommt dieselbe Antwort wie vom JDownloader.
(function () {
  if (window.__mediasortCnl) return;
  window.__mediasortCnl = true;
  var RE = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\]):9666(\/|$)/i;

  function abs(u) {
    try { return new URL(String(u), location.href).href; } catch (e) { return String(u); }
  }
  function isCnl(u) { return u != null && RE.test(abs(u)); }

  function toObj(body) {
    var o = {};
    try {
      if (!body) return o;
      if (typeof body === "string") new URLSearchParams(body).forEach(function (v, k) { o[k] = v; });
      else if (body instanceof URLSearchParams) body.forEach(function (v, k) { o[k] = v; });
      else if (window.FormData && body instanceof FormData) body.forEach(function (v, k) { if (typeof v === "string") o[k] = v; });
    } catch (e) {}
    return o;
  }

  // Antwort wie der JDownloader; CNL-Daten gehen an die App
  function handle(url, data) {
    var u;
    try { u = new URL(abs(url)); } catch (e) { return ""; }
    u.searchParams.forEach(function (v, k) { if (!(k in data)) data[k] = v; });
    var p = u.pathname;
    if (/\/flash\/add/.test(p)) {
      if (data.crypted || data.urls) {
        try { window.MediasortCnl.send(JSON.stringify(data)); } catch (e) {}
      }
      return "success\r\n";
    }
    if (/jdcheck\.js$/.test(p)) return "jdownloader=true;\nvar version='43307';\n";
    if (/crossdomain\.xml$/.test(p)) return '<?xml version="1.0"?><cross-domain-policy><allow-access-from domain="*" /></cross-domain-policy>';
    if (/^\/flash\/?$/.test(p)) return "JDownloader\r\n";
    return "";
  }

  // fetch
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var url = input && typeof input === "object" && "url" in input ? input.url : input;
      if (!isCnl(url)) return origFetch.apply(this, arguments);
      var bodyP = init && init.body != null ? Promise.resolve(init.body)
        : (input && typeof input === "object" && input.text ? input.text().catch(function () { return ""; }) : Promise.resolve(""));
      return bodyP.then(function (b) {
        var text = handle(url, toObj(b));
        return new Response(text, {status: 200, headers: {"Content-Type": "text/plain", "Access-Control-Allow-Origin": "*"}});
      });
    };
  }

  // XMLHttpRequest
  var XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__msUrl = isCnl(url) ? url : null;
    if (this.__msUrl) return;
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (!this.__msUrl) return XS.apply(this, arguments);
    var x = this, text = handle(x.__msUrl, toObj(body));
    var def = function (k, v) { try { Object.defineProperty(x, k, {configurable: true, get: function () { return v; }}); } catch (e) {} };
    setTimeout(function () {
      def("readyState", 4); def("status", 200); def("statusText", "OK");
      def("responseText", text); def("response", text); def("responseURL", abs(x.__msUrl));
      ["readystatechange", "load", "loadend"].forEach(function (t) {
        try { var h = x["on" + t]; if (typeof h === "function") h.call(x, new Event(t)); } catch (e) {}
        try { x.dispatchEvent(new Event(t)); } catch (e) {}
      });
    }, 30);
  };
  XMLHttpRequest.prototype.setRequestHeader = (function (orig) {
    return function () { if (this.__msUrl) return; return orig.apply(this, arguments); };
  })(XMLHttpRequest.prototype.setRequestHeader);

  // Formulare (auch per Skript abgeschickt)
  function formData(form) {
    var o = {};
    try { new FormData(form).forEach(function (v, k) { if (typeof v === "string") o[k] = v; }); } catch (e) {}
    try {
      [].forEach.call(form.elements || [], function (el) { if (el.name && !(el.name in o) && el.value != null) o[el.name] = String(el.value); });
    } catch (e) {}
    return o;
  }
  function action(form) { return form.getAttribute("action") ? form.action : location.href; }
  function fakeLoad(form) {
    // Wartet die Seite auf das Ziel-iframe, bekommt sie ein "load"
    try {
      var t = form.getAttribute("target");
      var f = t && document.querySelector('iframe[name="' + t + '"]');
      if (f) setTimeout(function () { f.dispatchEvent(new Event("load")); }, 50);
    } catch (e) {}
  }
  var FS = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (isCnl(action(this))) { handle(action(this), formData(this)); fakeLoad(this); return; }
    return FS.apply(this, arguments);
  };
  if (HTMLFormElement.prototype.requestSubmit) {
    var RS = HTMLFormElement.prototype.requestSubmit;
    HTMLFormElement.prototype.requestSubmit = function () {
      if (isCnl(action(this))) { handle(action(this), formData(this)); fakeLoad(this); return; }
      return RS.apply(this, arguments);
    };
  }
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f && f.tagName === "FORM" && isCnl(action(f))) {
      e.preventDefault();
      handle(action(f), formData(f));
      fakeLoad(f);
    }
  }, true);

  // sendBeacon
  if (navigator.sendBeacon) {
    var SB = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      if (isCnl(url)) { handle(url, toObj(data)); return true; }
      return SB(url, data);
    };
  }

  // Notlösung, falls die Seite kein erkennbares Click'n'Load nutzt: Schlüssel/Daten-Kandidaten sammeln
  window.__mediasortScan = function () {
    var forms = [].filter.call(document.querySelectorAll("form"), function (f) { return /9666|flash\/add/.test(f.getAttribute("action") || ""); });
    if (forms.length) {
      var d = formData(forms[0]);
      if (d.crypted || d.urls) { window.MediasortCnl.send(JSON.stringify(d)); return "form"; }
    }
    var a = [], s = [];
    [].forEach.call(document.querySelectorAll("*"), function (e) {
      for (var i = 0; i < e.attributes.length; i++) {
        var x = e.attributes[i];
        if (/^on|^data-|^value$|^href$/.test(x.name)) a.push(x.value);
      }
      if (e.tagName === "SCRIPT") s.push(e.textContent);
    });
    var t = a.concat(s).join(" "), k = {}, c = {};
    (t.match(/\b[0-9a-fA-F]{32}\b/g) || []).forEach(function (x) { k[x] = 1; });
    (t.match(/[A-Za-z0-9+\/]{24,}={0,2}/g) || []).forEach(function (x) { if (x.length % 4 === 0 && !/^[0-9a-fA-F]+$/.test(x)) c[x] = 1; });
    k = Object.keys(k).slice(0, 40); c = Object.keys(c).slice(0, 80);
    if (k.length && c.length) { window.MediasortCnl.send(JSON.stringify({keys: k.join(","), crypts: c.join(",")})); return "scan"; }
    return "none";
  };
})();
