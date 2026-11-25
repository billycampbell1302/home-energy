// Service worker: lets the dashboard install as an app and receive notifications. Always uses live data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data.json(); } catch (_) { d = {title: 'Home Energy', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Home Energy', {body: d.body || '', icon: '/static/icon-192.png', data: {url: d.url || '/'}}));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({type: 'window'}).then((cs) => {
    if (!cs.length) return self.clients.openWindow(url);
    // tell the open app what was tapped (e.g. the comfort check-in) and bring it forward
    cs[0].postMessage({open: url});
    return cs[0].focus();
  }));
});
