// Service worker — deliberately does almost nothing.
//
// Chrome will not offer "Install app" without a registered worker that has a
// fetch handler, so this exists to make the PWA installable. What it does NOT
// do is cache: Continuum is a live view of a machine that is only reachable
// over Tailscale anyway, so a cached shell would only ever show stale routine
// state and a chat that cannot send. Serving yesterday's dashboard is worse
// than honestly failing to load.
//
// It is also the hook push notifications would attach to later (a 'push'
// listener here, plus a subscription on the server).

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  // Straight pass-through to the network.
  event.respondWith(fetch(event.request));
});
