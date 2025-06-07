// service-worker.js
// Version of the cache
const CACHE_NAME = 'tunejam-audio-cache-v1';
const urlsToCache = [
    '/',
    '/index.html',
    '/manifest.json',
    // Add other critical static assets here if any, e.g.,
    // '/styles.css',
    // '/script.js',
    // '/images/logo.png'
];

// Install event: cache static assets
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then((cache) => {
                console.log('Opened cache');
                return cache.addAll(urlsToCache);
            })
            .catch(error => {
                console.error('Failed to cache static assets:', error);
            })
    );
});

// Activate event: clean up old caches
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('Deleting old cache:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
});

// Fetch event: intercept requests and serve from cache or network
self.addEventListener('fetch', (event) => {
    // Only intercept specific audio proxy requests for caching
    const audioProxyRegex = /^\/(proxy_googledrive_audio|proxy_youtube_audio)\/.+/;

    if (audioProxyRegex.test(event.request.url)) {
        event.respondWith(
            caches.open(CACHE_NAME).then(async (cache) => {
                // Try to get from cache first
                const cachedResponse = await cache.match(event.request);
                if (cachedResponse) {
                    console.log('[Service Worker] Serving from cache:', event.request.url);
                    return cachedResponse;
                }

                // If not in cache, fetch from network
                console.log('[Service Worker] Fetching from network:', event.request.url);
                try {
                    const networkResponse = await fetch(event.request);
                    // Check if the response is valid before caching
                    if (networkResponse && networkResponse.status === 200) {
                        // Cache the new response (clone to use once for cache, once for response)
                        cache.put(event.request, networkResponse.clone());
                        console.log('[Service Worker] Cached new response:', event.request.url);
                    }
                    return networkResponse;
                } catch (error) {
                    console.error('[Service Worker] Fetch failed:', event.request.url, error);
                    // Fallback for network failures (e.g., show an offline page or error)
                    // For audio, this might mean the track fails to play
                    return new Response(JSON.stringify({ error: 'Offline or Network Error' }), {
                        status: 503,
                        headers: { 'Content-Type': 'application/json' }
                    });
                }
            })
        );
    } else {
        // For all other requests (non-audio proxy), just let them go to network
        event.respondWith(fetch(event.request));
    }
});
