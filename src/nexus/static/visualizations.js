import { t } from './i18n.js?v=145';
import { fileUrl, filePreviewUrl } from './files.js?v=145';

const frames = new WeakMap();
const theme = () => getComputedStyle(document.documentElement).colorScheme === 'dark' ? 'dark' : 'light';
const resources = 'blob: data: https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://esm.sh https://fonts.bunny.net https://fonts.googleapis.com https://fonts.gstatic.com https://unpkg.com';
const policy = `default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' ${resources} https://persistent.oaistatic.com/plugins/visualize/; style-src 'unsafe-inline' ${resources} https://persistent.oaistatic.com/plugins/visualize/charts/; img-src ${resources}; font-src ${resources}; media-src ${resources}; worker-src blob:; connect-src blob: data:; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`;
let runtime;

async function readText(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(30_000) });
  if (!response.ok) {
    const detail = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : null;
    throw new Error(detail?.error?.message || t("加载失败（HTTP {0}）", response.status));
  }
  return response.text();
}

function loadRuntime() {
  return runtime ||= Promise.all([
    readText('/static/vendor/visualize-template.html?v=145'),
    readText('/static/vendor/visualize-calendar.js?v=145'),
    readText('/static/vendor/lucide.js?v=145'),
  ]).then(([template, calendar, icons]) => [
    template.replace('<!--__NEXUS_VISUALIZATION_ICONS__-->', () => `<script>${icons}</script>`), calendar,
  ]).catch(error => { runtime = null; throw error; });
}

// Runs before the fragment inside an opaque-origin sandbox.
function bridge(initial) {
  let port;
  const pending = [];
  const post = message => port ? port.postMessage(message) : pending.push(message);
  const applyTheme = value => {
    document.documentElement.dataset.theme = value;
    window.openai.theme = window.openai.visualizationTheme = value;
    window.dispatchEvent(new CustomEvent('openai:set_globals', { detail: { globals: window.openai } }));
  };
  window.openai = {
    widgetState: initial.widgetState,
    statePersistence: 'local', stateModelContext: 'none', visualizationStyleVariables: {},
    async setWidgetState(state) {
      const snapshot = { modelContent: null, privateContent: null, ...state };
      window.openai.widgetState = snapshot;
      post({ type: 'state', state: snapshot });
      window.dispatchEvent(new CustomEvent('openai:set_globals', { detail: { globals: { widgetState: snapshot } } }));
    },
    async sendFollowUpMessage({ prompt, title }) {
      if (navigator.userActivation.isActive) post({ type: 'follow-up', prompt, title });
    },
    async openExternal({ href }) {
      if (navigator.userActivation.isActive) post({ type: 'external', href });
    },
  };
  const measure = () => post({ type: 'height', height: Math.ceil(Math.max(document.body.scrollHeight, document.body.getBoundingClientRect().height)) });
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'nexus/visualization-init' || port) return;
    port = event.ports[0];
    port.onmessage = ({ data }) => {
      if (data.type === 'theme') applyTheme(data.theme);
      if (data.type === 'measure') measure();
    };
    for (const message of pending.splice(0)) port.postMessage(message);
    measure();
  });
  new ResizeObserver(measure).observe(document.body);
  document.addEventListener('click', event => {
    const anchor = event.target.closest('a[href]');
    if (!anchor || event.defaultPrevented || anchor.getAttribute('href').startsWith('#')) return;
    event.preventDefault();
    window.openai.openExternal({ href: anchor.href });
  });
  applyTheme(initial.theme);
  parent.postMessage({ type: 'nexus/visualization-ready' }, '*');
}

window.addEventListener('message', event => {
  if (event.data?.type !== 'nexus/visualization-ready') return;
  const entry = frames.get(event.source);
  if (!entry || entry.port) return;
  const channel = new MessageChannel();
  entry.port = channel.port1;
  channel.port1.onmessage = ({ data }) => {
    if (!entry.frame.isConnected) { channel.port1.close(); return; }
    if (data.type === 'height' && Number.isFinite(data.height) && data.height > 0) entry.frame.style.height = `${data.height}px`;
    if (data.type === 'state') {
      try { localStorage.setItem(entry.key, JSON.stringify(data.state)); } catch { /* Storage may be unavailable. */ }
    }
    if (data.type === 'follow-up' && typeof data.prompt === 'string' && window.confirm(`${data.title || t("发送到会话")}\n\n${data.prompt}`)) {
      entry.frame.dispatchEvent(new CustomEvent('nexus/visualization-follow-up', { bubbles: true, detail: { prompt: data.prompt } }));
    }
    if (data.type === 'external' && typeof data.href === 'string' && /^https?:\/\//i.test(data.href)) window.open(data.href, '_blank', 'noopener,noreferrer');
  };
  event.source.postMessage({ type: 'nexus/visualization-init' }, '*', [channel.port2]);
});

function syncTheme() {
  for (const frame of document.querySelectorAll('.visualization > iframe')) frames.get(frame.contentWindow)?.port?.postMessage({ type: 'theme', theme: theme() });
}
new MutationObserver(syncTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncTheme);

export function mountVisualizations(container, { machineId } = {}) {
  if (!machineId) return;
  for (const element of container.querySelectorAll('[data-visualization-path]')) {
    if (element.dataset.mounted) continue;
    element.dataset.mounted = 'true';
    const path = element.dataset.visualizationPath;
    const key = `nexus-visualization:${machineId}:${element.closest('[data-message-id]')?.dataset.messageId}:${path}`;
    const load = async () => {
      element.innerHTML = `<span class="spinner" role="status" aria-label="${t("正在加载可视化")}" data-i18n-aria-label="正在加载可视化"></span>`;
      try {
        const [fragment, [template, calendar]] = await Promise.all([readText(fileUrl(machineId, path)), loadRuntime()]);
        if (!element.isConnected) return;
        let widgetState = null;
        try { widgetState = JSON.parse(localStorage.getItem(key)); } catch { /* Start with the fragment defaults. */ }
        const initial = JSON.stringify({ theme: theme(), widgetState }).replace(/</g, '\\u003c');
        const calendarScript = /<viz-calendar\b/i.test(fragment) && !/\/visualize\/widgets\/[^/]+\/(?:calendar|element)\.js/.test(fragment) ? `<script>${calendar}</script>` : '';
        const frame = document.createElement('iframe');
        frame.title = element.dataset.visualizationTitle;
        frame.setAttribute('sandbox', 'allow-scripts');
        frame.referrerPolicy = 'no-referrer';
        frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="${policy}"></head><body><script>(${bridge.toString()})(${initial})</script>${template.replace('<!--__INLINE_VISUALIZATION_FRAGMENT__-->', () => calendarScript + fragment)}</body></html>`;
        element.replaceChildren(frame);
        frames.set(frame.contentWindow, { frame, key });
      } catch (error) {
        if (!element.isConnected) return;
        const retry = document.createElement('button');
        retry.className = 'button subtle';
        retry.textContent = t("加载失败，重试");
        retry.dataset.tooltip = error.message;
        retry.addEventListener('click', load);
        const file = document.createElement('a');
        file.href = filePreviewUrl(machineId, path);
        file.textContent = t("打开文件");
        element.replaceChildren(retry, file);
      }
    };
    load();
  }
}

// Keep live frames attached while streaming text and message controls change.
export function patchVisualizationMarkdown(current, next) {
  const key = node => JSON.stringify([node.dataset.visualizationPath, node.dataset.visualizationMode]);
  const visualizations = new Map();
  for (const node of current.children) if (node.matches('[data-visualization-path]')) {
    if (!visualizations.has(key(node))) visualizations.set(key(node), []);
    visualizations.get(key(node)).push(node);
  }
  let before = current.firstChild;
  for (const child of [...next.childNodes]) {
    const previous = child.nodeType === Node.ELEMENT_NODE && child.matches('[data-visualization-path]') && visualizations.get(key(child))?.shift();
    if (previous?.parentNode === current) {
      while (before && before !== previous) { const following = before.nextSibling; before.remove(); before = following; }
      previous.dataset.visualizationTitle = child.dataset.visualizationTitle;
      const frame = previous.querySelector('iframe');
      if (frame) frame.title = child.dataset.visualizationTitle;
      before = previous.nextSibling;
    } else if (before?.isEqualNode(child)) before = before.nextSibling;
    else current.insertBefore(child, before);
  }
  while (before) { const following = before.nextSibling; before.remove(); before = following; }
}
