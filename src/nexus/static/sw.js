const CACHE = 'nexus-shell-v145';
const PREFERENCES = 'nexus-preferences';
const ASSETS = [
  '/', '/offline.html', '/manifest.webmanifest',
  '/static/style.css?v=145', '/static/app.js?v=145', '/static/api.js?v=145', '/static/views.js?v=145',
  '/static/i18n.js?v=145', '/static/en.js?v=145', '/static/manifest.zh-CN.webmanifest?v=145',
  '/static/composer.js?v=145', '/static/select.js?v=145', '/static/pwa.js?v=145', '/static/icons.js?v=145', '/static/files.js?v=145', '/static/file-browser.js?v=145',
  '/static/goals.js?v=145',
  '/static/throughput.js?v=145', '/static/markdown.js?v=145', '/static/layout.js?v=145', '/static/machine-filter.js?v=145',
  '/static/context-menu.js?v=145', '/static/groups.js?v=145',
  '/static/tooltip.js?v=145', '/static/accounts.js?v=145', '/static/features.js?v=145',
  '/static/conversation.js?v=145', '/static/conversation-state.js?v=145', '/static/conversation-controller.js?v=145',
  '/static/conversation-actions.js?v=145',
  '/static/context.js?v=145', '/static/side.js?v=145', '/static/developer.js?v=145', '/static/terminals.js?v=145',
  '/static/vendor/xterm.js?v=145', '/static/vendor/xterm-addon-fit.js?v=145', '/static/vendor/xterm.css?v=145',
  '/static/visualizations.js?v=145', '/static/vendor/visualize-template.html?v=145', '/static/vendor/visualize-calendar.js?v=145', '/static/vendor/lucide.js?v=145',
  '/static/vendor/marked.js?v=145', '/static/vendor/purify.js?v=145',
  '/static/mermaid.js?v=145', '/static/vendor/mermaid.js?v=145',
  '/static/vendor/katex.js?v=145', '/static/vendor/katex.css', '/static/favicon.svg?v=145',
  '/static/icon-192.png?v=145', '/static/icon-512.png?v=145', '/static/icon-maskable-512.png?v=145',
  '/static/apple-touch-icon.png?v=145',
];

self.addEventListener('message', event => {
  if (event.data?.type !== 'nexus/language') return;
  event.waitUntil(caches.open(PREFERENCES).then(cache => cache.put('/language', new Response(event.data.language))));
});

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS.map(asset => new Request(asset, { cache: 'reload' })))).then(() => self.skipWaiting()));
});

self.addEventListener('push', event => {
  const message = event.data?.json() || {};
  event.waitUntil((async () => {
    const preferences = await caches.open(PREFERENCES);
    const saved = await preferences.match('/language');
    const language = saved ? await saved.text() : navigator.language;
    const english = !language.toLowerCase().startsWith('zh');
    const title = message.title || '任务已完成';
    const titles = { '任务已完成': 'Task completed', 'Nexus 测试通知': 'Nexus test notification' };
    const body = english && message.body === '任务完成后会在这里提醒你。' ? 'You’ll be notified here when a task finishes.'
      : english ? message.body?.replace(/ · Codex 会话$/, ' · Codex session') : message.body;
    let error;
    try {
      await self.registration.showNotification(english ? titles[title] || title : title, {
        body, tag: message.tag, renotify: Boolean(message.tag), icon: '/static/icon-192.png?v=145',
        badge: '/static/icon-192.png?v=145', data: { url: message.url || '/' },
      });
    } catch (cause) {
      if (!message.testId) throw cause;
      error = cause.message;
    }
    if (message.testId) {
      for (const client of await self.clients.matchAll({ type: 'window', includeUncontrolled: true })) {
        client.postMessage({ type: 'nexus/push-test', testId: message.testId, error });
      }
    }
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/', self.location.origin);
  event.waitUntil((async () => {
    for (const client of await self.clients.matchAll({ type: 'window', includeUncontrolled: true })) {
      if (new URL(client.url).origin === target.origin) {
        await client.focus();
        client.postMessage({ type: 'nexus/open-thread', url: target.href });
        return;
      }
    }
    await self.clients.openWindow(target.href);
  })());
});

self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil((async () => {
    const subscription = event.newSubscription || await self.registration.pushManager.subscribe(event.oldSubscription.options);
    await fetch('/api/push/subscriptions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(subscription),
    });
    if (event.oldSubscription && event.oldSubscription.endpoint !== subscription.endpoint) {
      await fetch('/api/push/subscriptions', {
        method: 'DELETE', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: event.oldSubscription.endpoint }),
      });
    }
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('nexus-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode !== 'navigate' && !url.pathname.startsWith('/static/')) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(request, { cache: 'no-cache' });
      if (response.ok) await cache.put(request, response.clone());
      return response;
    } catch {
      return cache.match(request.mode === 'navigate' ? '/offline.html' : request);
    }
  })());
});
