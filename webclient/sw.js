// Patchbay service worker.
//
// Caching policy, by request kind:
//   - navigation/HTML requests -> network-first, falling back to the cached
//     shell when offline. index.html changes on every deploy with no
//     version stamp (this repo has no build step and no VERSION file), so a
//     cache-first shell would silently serve a stale cockpit after a pull.
//   - same-origin static GETs (theme CSS/JSON, avatar assets, icons,
//     manifest.json, audio clips) -> stale-while-revalidate.
//   - DYNAMIC_PATHS below, any non-GET, and any cross-origin request -> left
//     alone entirely (no respondWith call), so they reach the network
//     exactly as if this service worker did not exist. The control
//     WebSocket isn't a fetch and is never routed through here either way.
const SHELL_CACHE = "patchbay-shell-v1";
const ASSET_CACHE = "patchbay-assets-v1";
const OFFLINE_SHELL = "./index.html";

// Server-side routes whose body reflects live state (see webclient/serve.py
// and webclient/ha_deck.py) -- must never be cached.
//
// Deliberately absolute, not derived from registration.scope: the client
// itself fetches these paths absolutely -- fetch("/ha/states")
// (index.html:7745), fetch("/ha/intent") (index.html:7140),
// new EventSource("/ha/stream") (index.html:7583) -- and serve.py's own
// MODEL_PATHS is absolute too, so the whole app already assumes root
// mounting. A scope-relative list here would look for e.g. "/sub/models"
// while the client still requests "/models", silently start caching the
// live endpoints this set exists to exclude, and not even help subdirectory
// deployments since nothing else in the app supports one. Anyone adding a
// dynamic route, or actually adding subdirectory support, must change BOTH
// this list and the client's fetch calls, or neither.
const DYNAMIC_PATHS = new Set(["/models", "/v1/models", "/ha/stream", "/ha/states", "/ha/intent"]);

self.addEventListener("install", (event) => {
  // Deliberately NO self.skipWaiting() here. This is a live voice
  // instrument: an update that activates itself unprompted fires
  // "controllerchange" in every open tab, and index.html reloads on that --
  // dropping the WebSocket and killing an in-flight conversation, possibly
  // mid-sentence, with no warning. A first install has no active worker to
  // wait behind, so it still activates on its own regardless; only a
  // genuine update (a worker already controlling the page) now waits for
  // the user to click "Update ready" in the service panel, which calls
  // skipWaiting() via the "message" listener below. Do not add this back.
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.add(OFFLINE_SHELL)));
});

self.addEventListener("activate", (event) => {
  // Deliberately no clients.claim() here: claiming an already-open,
  // uncontrolled page fires `controllerchange` on that page too, which
  // would be indistinguishable from a real update and fire the reload in
  // index.html's "controllerchange" listener on every first visit. Any
  // navigation (including the reload the update flow already does) picks
  // up the active worker on its own once this handler has run.
  const keep = new Set([SHELL_CACHE, ASSET_CACHE]);
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => !keep.has(n)).map((n) => caches.delete(n))))
  );
});

function isNavigationRequest(request) {
  return request.mode === "navigate" || (request.headers.get("accept") || "").includes("text/html");
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (DYNAMIC_PATHS.has(url.pathname)) return;

  if (isNavigationRequest(request)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Only a genuine, complete app response overwrites the offline
          // shell -- an error page (SimpleHTTPRequestHandler and serve.py
          // both answer a 404 with Content-Type: text/html, which passes
          // isNavigationRequest()) must never poison it, and opaque/
          // redirected responses (type !== "basic") aren't safe to
          // cache.put() at all.
          if (response.ok && response.status === 200 && response.type === "basic") {
            const copy = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(OFFLINE_SHELL, copy));
          }
          return response;
        })
        .catch(() => caches.match(OFFLINE_SHELL).then((cached) => cached || Response.error()))
    );
    return;
  }

  event.respondWith(
    caches.open(ASSET_CACHE).then(async (cache) => {
      const cached = await cache.match(request);
      const network = fetch(request)
        .then((response) => {
          if (response.ok) cache.put(request, response.clone());
          return response;
        })
        // Falls through to `undefined` when this asset was never cached AND
        // the network fails -- respondWith() requires a real Response.
        .catch(() => cached || Response.error());
      return cached || network;
    })
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skipWaiting" || (event.data && event.data.type === "SKIP_WAITING")) {
    self.skipWaiting();
  }
});
