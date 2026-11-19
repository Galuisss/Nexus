import { t, locale } from './i18n.js?v=145';
import { h } from './views.js?v=145';
import { closeSelectMenu } from './select.js?v=145';

export function setupMachineFilter(onChange, { id = 'machine-filter', multiple = true, storageKey = multiple ? 'nexus-machine-filter' : null } = {}) {
  const trigger = document.getElementById(id);
  const label = document.getElementById(`${id}-label`);
  const menu = document.getElementById(`${id}-menu`);
  const options = document.getElementById(`${id}-options`);
  let selection = null;
  let machines = [];
  let signature;
  try {
    const saved = storageKey && JSON.parse(localStorage.getItem(storageKey));
    if (saved) selection = new Set(saved);
  } catch { /* Use all machines. */ }

  const includes = id => selection === null || selection.has(id);
  const selectedMachines = () => machines.filter(machine => includes(machine.id));

  function render() {
    const selected = selectedMachines();
    label.textContent = !multiple ? selected[0]?.name || ''
      : selection === null || (machines.length && selected.length === machines.length) ? t("全部机器")
      : !selected.length ? t("未选择机器") : selected.length === 1 ? selected[0].name : t("已选 {0} 台", selected.length);
    trigger.dataset.tooltip = selected.map(machine => machine.name).join(locale() === 'en' ? ', ' : '、');
    trigger.disabled = !machines.length;
    options.querySelectorAll('input').forEach(input => { input.checked = includes(input.value); });
  }

  function change(ids) {
    selection = multiple && ids.length === machines.length ? null : new Set(multiple ? ids : ids.slice(0, 1));
    if (storageKey) localStorage.setItem(storageKey, JSON.stringify(selection === null ? null : [...selection]));
    render();
    if (!multiple) menu.hidePopover();
    onChange(selectedMachines());
  }

  trigger.addEventListener('click', () => closeSelectMenu());
  menu.addEventListener('toggle', () => {
    const open = menu.matches(':popover-open');
    trigger.setAttribute('aria-expanded', String(open));
    if (!open) return;
    const rect = trigger.getBoundingClientRect();
    menu.style.minWidth = `${Math.min(Math.max(rect.width, 240), innerWidth - 16)}px`;
    menu.style.maxHeight = `${Math.min(360, innerHeight - 16)}px`;
    menu.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(rect.bottom + 6, innerHeight - menu.offsetHeight - 8))}px`;
    menu.querySelector('input, button')?.focus({ preventScroll: true });
  });
  options.addEventListener('change', () => change([...options.querySelectorAll('input:checked')].map(input => input.value)));
  options.addEventListener('click', event => {
    if (!multiple && event.target.matches('input:checked')) menu.hidePopover();
  });
  menu.addEventListener('click', event => {
    const action = event.target.closest('[data-machine-action]')?.dataset.machineAction;
    if (action) change(action === 'all' ? machines.map(machine => machine.id) : action === 'invert' ? machines.filter(machine => !includes(machine.id)).map(machine => machine.id) : []);
  });
  window.addEventListener('resize', () => { if (menu.matches(':popover-open')) menu.hidePopover(); });

  render();

  return {
    includes,
    selectedMachines,
    update(next, selectedId) {
      machines = next;
      if (!multiple) {
        const current = machines.find(machine => machine.id === selectedId) || selectedMachines()[0] || machines[0];
        selection = new Set(current ? [current.id] : []);
      }
      const nextSignature = JSON.stringify([locale(), machines.map(machine => [machine.id, machine.name])]);
      if (signature !== nextSignature) {
        signature = nextSignature;
        options.innerHTML = machines.length ? machines.map(machine => `<label class="machine-filter-option"><input type="${multiple ? 'checkbox' : 'radio'}" name="${h(id)}" value="${h(machine.id)}"><span>${h(machine.name)}</span></label>`).join('') : `<div class="list-empty" data-i18n="暂无机器">${t("暂无机器")}</div>`;
      }
      render();
    },
  };
}
