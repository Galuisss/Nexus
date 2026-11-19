import { t } from './i18n.js?v=145';
export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, options = {}) {
  const { timeout = 30_000, ...request } = options;
  const signal = AbortSignal.timeout(timeout);
  const form = request.body instanceof FormData;
  try {
    const response = await fetch(`/api${path}`, {
      ...request, signal,
      headers: { ...(form ? {} : { 'Content-Type': 'application/json' }), ...request.headers },
      body: request.body === undefined ? undefined : form ? request.body : JSON.stringify(request.body),
    });
    if (!response.ok && !response.headers.get('Content-Type')?.includes('application/json')) {
      throw new ApiError(t("请求失败（HTTP {0} {1}）", response.status, response.statusText), response.status);
    }
    const payload = response.status === 204 ? {} : await response.json();
    if (!response.ok) {
      const error = payload.error || payload.detail || {};
      throw new ApiError(t(typeof error === 'string' ? error : error.message || "操作未完成"), response.status, error);
    }
    return payload;
  } catch (error) {
    if (signal.aborted) throw new ApiError(t("请求超时，请重试"), 504);
    throw error;
  }
}

export function query(values) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, value);
  }
  return params.size ? `?${params}` : '';
}

export function threadPath(machineId, threadId) {
  return `/machines/${encodeURIComponent(machineId)}/threads/${encodeURIComponent(threadId)}`;
}

export function openEvents(onEvent, onConnection = () => {}) {
  let source;
  let closed = false;
  let timer;

  function connect() {
    source?.close();
    source = new EventSource('/api/events');
    source.addEventListener('nexus', event => onEvent(JSON.parse(event.data)));
    source.onmessage = event => onEvent(JSON.parse(event.data));
    source.onopen = () => onConnection(true);
    source.onerror = () => onConnection(false);
  }

  async function checkConnection() {
    try {
      await api('/config', { cache: 'no-store', timeout: 5000 });
      if (!closed && source.readyState === EventSource.CLOSED) connect();
    } catch {
      if (!closed) {
        source.close();
        onConnection(false);
      }
    } finally {
      if (!closed) timer = setTimeout(checkConnection, 10000);
    }
  }

  connect();
  timer = setTimeout(checkConnection, 10000);
  return {
    close() {
      closed = true;
      clearTimeout(timer);
      source.close();
    },
  };
}
