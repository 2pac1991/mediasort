// Saugmeister Service Worker: zeigt eine Hinweisseite, wenn der Server nicht erreichbar ist (z. B. Tailscale aus).
const CACHE = "mediasort-offline-v3";
const OFFLINE = "/offline.html";

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll([OFFLINE, "/apple-touch-icon.png"])));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  // Seitenaufrufe: ohne Verbindung die Hinweisseite zeigen
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
  // Icon der Hinweisseite aus dem Speicher; API-Anfragen laufen immer direkt zum Server
  } else if (url.origin === location.origin && url.pathname === "/apple-touch-icon.png") {
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
  }
});
