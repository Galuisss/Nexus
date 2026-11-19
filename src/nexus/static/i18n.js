import english from './en.js?v=145';

const storageKey = 'nexus-language';
const supported = ['en', 'zh-CN'];
const browserLanguage = (navigator.languages?.[0] || navigator.language || 'en').toLowerCase();
const saved = localStorage.getItem(storageKey);
let language = supported.includes(saved) ? saved : browserLanguage.startsWith('zh') ? 'zh-CN' : 'en';

export const locale = () => language;

// Chinese source messages also serve as the Simplified Chinese catalog.
export function t(message, ...values) {
  const translated = language === 'en' ? english[message] ?? message : message;
  const text = typeof translated === 'function' ? translated(...values) : translated;
  return text.replace(/\{(\d+)\}/g, (match, index) => index < values.length ? String(values[index] ?? '') : match);
}

export function translateDOM(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(node => {
    node.textContent = t(node.dataset.i18n, ...JSON.parse(node.dataset.i18nValues || '[]'));
  });
  for (const attribute of ['aria-label', 'placeholder', 'data-tooltip', 'alt', 'title']) {
    root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(node => {
      node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`)));
    });
  }
}

function applyLanguage() {
  document.documentElement.lang = language;
  const manifest = document.querySelector('link[rel="manifest"]');
  if (manifest) manifest.href = language === 'en' ? '/manifest.webmanifest' : '/static/manifest.zh-CN.webmanifest?v=145';
  translateDOM();
  delete document.documentElement.dataset.languagePending;
}

export function setLanguage(next) {
  if (!supported.includes(next) || next === language) return;
  language = next;
  localStorage.setItem(storageKey, language);
  applyLanguage();
  window.dispatchEvent(new Event('nexus:languagechange'));
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyLanguage, { once: true });
else applyLanguage();
