import { t } from './i18n.js?v=145';
import { icon } from './icons.js?v=145';
import { h } from './views.js?v=145';

const controls = new WeakMap();
const optionMarkup = new WeakMap();
let active;
let menuId = 0;

export function updateSelectOptions(select, markup) {
  if (optionMarkup.get(select) === markup) return;
  optionMarkup.set(select, markup);
  select.innerHTML = markup;
}

export function setSettingsDisabled(control, unavailable, saving) {
  control.classList.toggle('settings-pending', Boolean(saving && !unavailable));
  const disabled = Boolean(unavailable || saving);
  if (control.disabled !== disabled) control.disabled = disabled;
}

export function closeSelectMenu(restoreFocus = false) {
  if (!active) return;
  const { button, menu } = active;
  menu.hidePopover();
  button.setAttribute('aria-expanded', 'false');
  active = null;
  if (restoreFocus) button.focus();
}

function enhanceSelect(select) {
  if (controls.has(select)) return;
  const tooltips = !select.closest('.composer-model, .composer-effort');
  const control = document.createElement('div');
  control.className = 'select-control';
  select.before(control);
  control.append(select);
  select.hidden = true;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'select-trigger';
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-label', select.getAttribute('aria-label') || select.closest('.field')?.querySelector('span')?.textContent || t("选择"));
  const menu = document.createElement('div');
  menu.id = select.id ? `${select.id}-options` : `select-options-${++menuId}`;
  menu.className = 'select-menu';
  menu.setAttribute('popover', 'manual');
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', button.getAttribute('aria-label'));
  button.setAttribute('aria-controls', menu.id);
  const label = document.createElement('span');
  button.append(label);
  control.append(button, menu);
  const instance = { button, menu };
  controls.set(select, instance);

  const refresh = () => {
    const ariaLabel = select.getAttribute('aria-label') || select.closest('.field')?.querySelector('span')?.textContent || t("选择");
    button.setAttribute('aria-label', ariaLabel);
    menu.setAttribute('aria-label', ariaLabel);
    const option = select.selectedOptions[0];
    const text = option?.label || t("选择");
    const changed = label.textContent !== text;
    if (changed) label.textContent = text;
    if (tooltips) {
      const hint = select.dataset.tooltip || option?.label || '';
      if (button.dataset.tooltip !== hint) button.dataset.tooltip = hint;
    }
    button.classList.toggle('settings-pending', select.classList.contains('settings-pending'));
    if (button.disabled !== select.disabled) button.disabled = select.disabled;
    if (active === instance && (changed || button.disabled)) closeSelectMenu();
  };
  const open = (edge) => {
    if (button.disabled) return;
    closeSelectMenu();
    menu.innerHTML = [...select.options].map((option, index) => `<button type="button" role="option" tabindex="-1" data-option-index="${index}"${tooltips && option.dataset.tooltip ? ` data-tooltip="${h(option.dataset.tooltip)}"` : ''} aria-selected="${option.selected}" ${option.disabled ? 'disabled' : ''}><span>${h(option.label)}</span>${option.selected ? icon('check') : ''}</button>`).join('');
    active = instance;
    button.setAttribute('aria-expanded', 'true');
    menu.showPopover();
    const rect = button.getBoundingClientRect();
    const margin = 8;
    const below = window.innerHeight - rect.bottom - margin;
    const above = rect.top - margin;
    const upwards = button.closest('.composer') || (below < Math.min(280, menu.scrollHeight) && above > below);
    menu.style.maxHeight = `${Math.min(320, Math.max(0, (upwards ? above : below) - margin))}px`;
    menu.style.minWidth = `${Math.min(Math.max(rect.width, 160), window.innerWidth - margin * 2)}px`;
    menu.style.maxWidth = `${window.innerWidth - margin * 2}px`;
    menu.style.left = `${Math.max(margin, Math.min(rect.left, window.innerWidth - menu.offsetWidth - margin))}px`;
    menu.style.top = `${upwards ? rect.top - menu.offsetHeight - margin : rect.bottom + margin}px`;
    const options = [...menu.querySelectorAll('[role="option"]:not(:disabled)')];
    const target = edge === 'last' ? options.at(-1) : edge === 'first' ? options[0] : options.find(option => option.getAttribute('aria-selected') === 'true') || options[0];
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: 'nearest' });
  };
  button.addEventListener('click', () => active === instance ? closeSelectMenu() : open());
  button.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    open(event.key === 'End' ? 'last' : event.key === 'Home' ? 'first' : undefined);
  });
  menu.addEventListener('click', event => {
    const option = event.target.closest('[data-option-index]');
    if (!option || option.disabled) return;
    select.selectedIndex = Number(option.dataset.optionIndex);
    closeSelectMenu(true);
    refresh();
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeSelectMenu(true); return; }
    if (event.key === 'Tab') { closeSelectMenu(true); return; }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const options = [...menu.querySelectorAll('[role="option"]:not(:disabled)')];
    const index = options.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
    options[next]?.focus();
  });
  select.addEventListener('change', refresh);
  new MutationObserver(mutations => {
    if (active === instance && mutations.some(mutation => mutation.type !== 'attributes')) closeSelectMenu();
    refresh();
  }).observe(select, { attributes: true, childList: true, subtree: true, characterData: true });
  refresh();
}

export function enhanceSelects(root = document) {
  root.querySelectorAll('select').forEach(enhanceSelect);
}

document.addEventListener('pointerdown', event => {
  if (active && !active.button.contains(event.target) && !active.menu.contains(event.target)) closeSelectMenu();
});
window.addEventListener('resize', () => closeSelectMenu());
document.addEventListener('scroll', event => {
  if (active && !active.menu.contains(event.target)) closeSelectMenu();
}, true);
