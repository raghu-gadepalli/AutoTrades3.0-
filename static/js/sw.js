// static/js/sw.js
// UI CSV download refresh: 2026-07-27
const CACHE_NAME = "autotrades-p53-v2";

const PRECACHE_URLS = [
  // CSS
  "/static/css/style.css",
  "/static/css/oms.css",

  // Core JS
  "/static/js/default.js",

  // Dashboard JS
  "/static/js/watchlist.js",
  "/static/js/signals.js",
  "/static/js/snapshot.js",
  "/static/js/orders.js",
  "/static/js/positions.js",
  "/static/js/performance.js",
  "/static/js/derivatives.js",
  "/static/js/derivatives_widget.js",

  // Trade actions
  "/static/js/trade_create.js",
  "/static/js/trade_edit.js",
  "/static/js/trade_exit.js",

  // User pages
  "/static/js/user_funds.js",
  "/static/js/user_pref.js",
  "/static/js/user_profile.js",

  // OMS pages
  "/static/js/oms_clients.js",
  "/static/js/oms_dashboard.js",
  "/static/js/oms_funds.js",
  "/static/js/oms_new_order.js",
  "/static/js/oms_orders.js",
  "/static/js/oms_positions.js",

  // PWA
  "/static/manifest.json",

  // Icons
  "/static/images/favicon.ico",
  "/static/images/favicon.png",
  "/static/images/favicon-16.png",
  "/static/images/favicon-32.png",
  "/static/images/android-chrome-192x192.png",
  "/static/images/android-chrome-512x512.png",
  "/static/images/maskable-512x512.png",

  // Login/header images
  "/static/images/user-icon.png",
  "/static/images/google-icon.png",
  "/static/images/facebook-icon.png",
  "/static/images/broker-icon.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);

    for (const url of PRECACHE_URLS) {
      try {
        const req = new Request(url, { cache: "reload" });
        const res = await fetch(req);
        if (res && res.ok) {
          await cache.put(req, res.clone());
        } else {
          console.warn("[SW] skip precache", res && res.status, url);
        }
      } catch (err) {
        console.warn("[SW] failed to precache", url, err);
      }
    }

    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.map((key) => (key !== CACHE_NAME ? caches.delete(key) : null))
    );
    self.clients.claim();
  })());
});

function isSameOrigin(request) {
  try {
    return new URL(request.url).origin === self.location.origin;
  } catch {
    return false;
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request, { ignoreSearch: true });
  if (cached) return cached;

  const response = await fetch(request);

  try {
    const ct = response.headers.get("content-type") || "";
    const cacheable =
      isSameOrigin(request) &&
      response.ok &&
      /^(text\/css|application\/javascript|text\/javascript|image\/|application\/manifest\+json)/.test(ct);

    if (cacheable) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(request, response.clone());
    }
  } catch (_) {}

  return response;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  const accept = req.headers.get("accept") || "";
  const isHTML = req.mode === "navigate" || accept.includes("text/html");

  if (isHTML) {
    event.respondWith((async () => {
      try {
        return await fetch(new Request(req, { cache: "no-store" }));
      } catch {
        const cached = await caches.match(req, { ignoreSearch: true });
        return cached || new Response("Offline", {
          status: 503,
          statusText: "Offline"
        });
      }
    })());
    return;
  }

  if (!isSameOrigin(req)) return;

  // Only immutable/static assets use the application cache. Dashboard/OMS JSON
  // must always come from the server; caching it can mix Draft/Executed buckets
  // and leave positions/funds stale because query strings were ignored.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(req));
    return;
  }

  event.respondWith(fetch(new Request(req, { cache: "no-store" })));
});
