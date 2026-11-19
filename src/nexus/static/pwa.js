import { t, locale } from './i18n.js?v=145';
import { api } from './api.js?v=145';

let installPrompt;

const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone;
export const installLabel = () => standalone() ? t("已安装 Nexus") : t("安装 Nexus");

export function notificationMessages() {
  return standalone() ? {
    pending: t("等待 Nexus 接收…"),
    received: t("Nexus 已收到测试推送"),
    permission: t("请在通知设置中允许 Nexus 发送通知"),
    timeout: t("推送服务已接收，但 Nexus 尚未确认收到。请检查网络连接后重试"),
    displayError: t("Nexus 已收到推送，但请求显示通知失败"),
  } : {
    pending: t("等待浏览器接收…"),
    received: t("浏览器已收到测试推送"),
    permission: t("请在浏览器设置中允许 Nexus 发送通知"),
    timeout: t("推送服务已接收，但浏览器尚未确认收到。请检查浏览器与推送服务的网络连接后重试"),
    displayError: t("浏览器已收到推送，但请求显示通知失败"),
  };
}

export function setupPWA() {
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
  });
  window.addEventListener('appinstalled', () => { installPrompt = null; });
  if ('serviceWorker' in navigator && window.isSecureContext) {
    const syncLanguage = () => navigator.serviceWorker.ready.then(registration => {
      registration.active.postMessage({ type: 'nexus/language', language: locale() });
    });
    window.addEventListener('nexus:languagechange', syncLanguage);
    navigator.serviceWorker.addEventListener('controllerchange', syncLanguage);
    navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' }).then(registration => {
      syncLanguage();
      if (registration.active) return registration.update();
    }).catch(error => console.error('Nexus service worker:', error));
  }
  const viewport = window.visualViewport;
  if (viewport) {
    const resize = () => document.documentElement.style.setProperty('--app-height', `${viewport.height}px`);
    viewport.addEventListener('resize', resize);
    resize();
  }
}

export async function installApp(openModal) {
  if (standalone()) return;
  if (installPrompt) {
    const prompt = installPrompt;
    installPrompt = null;
    await prompt.prompt();
    return;
  }
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const instructions = !window.isSecureContext
    ? t("使用 Nexus 的 HTTPS 地址打开页面，再通过浏览器菜单安装。")
    : ios ? t("在 Safari 中打开 Nexus，轻点“分享”，选择“添加到主屏幕”，然后轻点“添加”。")
      : t("打开浏览器菜单，选择“安装应用”或“添加到主屏幕”。");
  openModal({ title: t("安装 Nexus"), body: `<p class="install-instructions">${instructions}</p>`, footer: false });
}

export async function notificationStatus() {
  if (!window.isSecureContext) return { label: t("通知需要 HTTPS"), disabled: true };
  if (!('PushManager' in window) || !('Notification' in window)) {
    return { label: t("请在支持通知的 PWA 中打开"), disabled: true };
  }
  if (Notification.permission === 'denied') return { label: t("通知权限已关闭"), disabled: true };
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  const enabled = Boolean(subscription) && Notification.permission === 'granted';
  return { label: enabled ? t("关闭完成通知") : t("开启完成通知"), enabled, disabled: false };
}

export async function toggleNotifications() {
  if (Notification.permission !== 'granted' && await Notification.requestPermission() !== 'granted') {
    throw new Error(notificationMessages().permission);
  }
  const registration = await navigator.serviceWorker.ready;
  const current = await registration.pushManager.getSubscription();
  if (current) {
    await api('/push/subscriptions', { method: 'DELETE', body: { endpoint: current.endpoint } });
    await current.unsubscribe();
    localStorage.removeItem('nexus-notifications');
    return false;
  }
  const { publicKey } = await api('/push/key');
  const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: publicKey });
  try {
    await api('/push/subscriptions', { method: 'POST', body: subscription.toJSON() });
  } catch (error) {
    await subscription.unsubscribe();
    throw error;
  }
  localStorage.setItem('nexus-notifications', 'enabled');
  return true;
}

export async function restoreNotifications() {
  if (!window.isSecureContext || !('PushManager' in window) || !('Notification' in window)
      || Notification.permission !== 'granted') return;
  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription && localStorage.getItem('nexus-notifications') === 'enabled') {
    const { publicKey } = await api('/push/key');
    subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: publicKey });
  }
  if (subscription) await api('/push/subscriptions', { method: 'POST', body: subscription.toJSON() });
}

export async function testNotification() {
  const messages = notificationMessages();
  if (Notification.permission !== 'granted') throw new Error(messages.permission);
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  if (!subscription) throw new Error(t("此设备的通知订阅已失效，请重新开启完成通知"));
  const testId = crypto.randomUUID();
  let onReceipt;
  let timer;
  const receipt = new Promise(resolve => {
    onReceipt = event => {
      if (event.data?.type === 'nexus/push-test' && event.data.testId === testId) resolve(event.data);
    };
    navigator.serviceWorker.addEventListener('message', onReceipt);
  });
  try {
    await api('/push/test', { method: 'POST', body: { endpoint: subscription.endpoint, testId } });
    const result = await Promise.race([
      receipt,
      new Promise(resolve => { timer = setTimeout(() => resolve(null), 15000); }),
    ]);
    if (!result) throw new Error(messages.timeout);
    if (result.error) throw new Error(`${messages.displayError}: ${result.error}`);
  } finally {
    clearTimeout(timer);
    navigator.serviceWorker.removeEventListener('message', onReceipt);
  }
}
