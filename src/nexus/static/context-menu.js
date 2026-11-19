import { t } from './i18n.js?v=145';
import { icon } from './icons.js?v=145';

const menu = document.createElement('div');
menu.className = 'context-menu';
menu.setAttribute('popover', 'manual');
menu.setAttribute('role', 'menu');
let returnFocus;

function closeMenu(restoreFocus = false) {
  if (!menu.matches(':popover-open')) return;
  const hadFocus = menu.contains(document.activeElement);
  menu.hidePopover();
  if (restoreFocus && hadFocus) returnFocus?.focus({ preventScroll: true });
}

document.addEventListener('pointerdown', event => {
  if (!menu.contains(event.target)) closeMenu();
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || !menu.matches(':popover-open')) return;
  event.preventDefault();
  closeMenu(true);
});

menu.addEventListener('keydown', event => {
  const buttons = [...menu.querySelectorAll('button')];
  const index = buttons.indexOf(document.activeElement);
  let next;
  switch (event.key) {
    case 'ArrowDown': next = (index + 1) % buttons.length; break;
    case 'ArrowUp': next = (index - 1 + buttons.length) % buttons.length; break;
    case 'Home': next = 0; break;
    case 'End': next = buttons.length - 1; break;
    default: return;
  }
  event.preventDefault();
  buttons[next].focus();
});

export function showContextMenu({ target, x, y, focus = true, className = '' }, items) {
  closeMenu(true);
  menu.className = `context-menu ${className}`;
  returnFocus = document.activeElement;
  const container = target.closest('dialog[open]') || document.body;
  if (menu.parentElement !== container) container.append(menu);
  menu.replaceChildren(...items.map(([symbol, label, action]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('role', 'menuitem');
    button.innerHTML = icon(symbol);
    button.append(label);
    button.addEventListener('click', () => {
      closeMenu(true);
      action();
    });
    return button;
  }));
  menu.showPopover();
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8))}px`;
  if (focus) menu.firstElementChild.focus({ preventScroll: true });
  const firstItem = menu.firstElementChild;
  return () => {
    if (menu.firstElementChild === firstItem) closeMenu(true);
  };
}

function selectedText(event) {
  const { target, clientX: x, clientY: y } = event;
  const keyboard = event.pointerType === '';
  if (target.matches('textarea, input')) {
    const { selectionStart: start, selectionEnd: end } = target;
    if (target.type === 'password' || start === null || start === end) return;
    if (keyboard) return target.value.slice(start, end);
    const caret = document.caretPositionFromPoint?.(x, y);
    if (caret?.offsetNode !== target || caret.offset < start || caret.offset > end) return;
    const rect = caret.getClientRect();
    if (rect && (y < rect.top || y > rect.bottom)) return;
    const rtl = getComputedStyle(target).direction === 'rtl';
    if (rect && ((caret.offset === start && (rtl ? x > rect.right : x < rect.left))
      || (caret.offset === end && (rtl ? x < rect.left : x > rect.right)))) return;
    return target.value.slice(start, end);
  }

  const selection = window.getSelection();
  const text = selection?.toString();
  if (!text) return;
  const range = selection.getRangeAt(0);
  if (keyboard || [...range.getClientRects()].some(rect => x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom)) return text;
}

export function copyText(text) {
  if (navigator.clipboard) return navigator.clipboard.writeText(text);
  const copy = event => {
    event.clipboardData.setData('text/plain', text);
    event.preventDefault();
  };
  document.addEventListener('copy', copy, { once: true });
  try {
    if (!document.execCommand('copy')) throw new Error(t("复制失败"));
  } finally {
    document.removeEventListener('copy', copy);
  }
}

export function setupTextContextMenu(toast) {
  document.addEventListener('contextmenu', event => {
    if (event.defaultPrevented || event.pointerType === 'touch' || event.sourceCapabilities?.firesTouchEvents) return;
    const { target, clientX: x, clientY: y } = event;
    if (target.closest('.terminal-screen')) return;
    event.preventDefault();
    if (target.closest('.context-menu')) return;
    const text = selectedText(event);
    if (!text) return;
    showContextMenu({ target, x, y, focus: event.pointerType === '' }, [
      ['search', t("使用 Google 搜索"), () => window.open(`https://www.google.com/search?q=${encodeURIComponent(text)}`, '_blank', 'noopener,noreferrer')],
      ['copy', t("复制"), async () => {
        try { await copyText(text); }
        catch (error) { toast(error.message, true); }
      }],
    ]);
  });
}
