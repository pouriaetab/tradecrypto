/* A deliberately empty service worker.
 *
 * It exists for one reason: a page is only installable to a phone's home screen
 * if it registers one. It caches NOTHING and every request goes straight to the
 * network.
 *
 * That is on purpose. This app has already lost days to stale code -- it ran a
 * build from 2026-09-12 for five days while reporting that a fix was live -- and
 * a caching service worker is the single best way to reproduce that bug in a
 * place where nobody thinks to look. A dashboard showing yesterday's positions
 * because a worker served them from cache is worse than a dashboard that does
 * not load at all, because it looks like it worked.
 *
 * If offline support is ever wanted, it should cache the shell only and never a
 * single /api response.
 */
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()))
