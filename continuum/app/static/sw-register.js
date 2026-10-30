// Service worker registration. This lived inline in index.html until the page
// grew a Content-Security-Policy: `script-src 'self'` is what stops an injected
// `<img onerror=…>` from running, and it can't tell our inline block from an
// attacker's. Moving the script to a file keeps the policy strict without
// needing a nonce that would have to be regenerated on every edit.
//
// Registered only over HTTPS (the Tailscale URL) or on localhost — the browser
// refuses a worker on a plain-http origin, and the desktop window loads over
// http://127.0.0.1, so guard rather than log an error there.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
