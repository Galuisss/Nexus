const tooltip = document.createElement('div');
tooltip.id = 'control-tooltip';
tooltip.className = 'tooltip';
tooltip.setAttribute('popover', 'manual');
tooltip.setAttribute('role', 'tooltip');

let target;
let timer;

function hide() {
  clearTimeout(timer);
  observer.disconnect();
  if (target) {
    const description = (target.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== tooltip.id).join(' ');
    if (description) target.setAttribute('aria-describedby', description);
    else target.removeAttribute('aria-describedby');
  }
  tooltip.hidePopover();
  target = null;
}

function show() {
  if (!target?.isConnected || !target.getClientRects().length || !target.dataset.tooltip) { hide(); return; }
  const container = target.closest('dialog[open]') || document.body;
  if (tooltip.parentElement !== container) container.append(tooltip);
  if (tooltip.textContent !== target.dataset.tooltip) tooltip.textContent = target.dataset.tooltip;
  if (!tooltip.matches(':popover-open')) tooltip.showPopover();
  const rect = target.getBoundingClientRect();
  const { width, height } = tooltip.getBoundingClientRect();
  const top = rect.top >= height + 16 ? rect.top - height - 8 : rect.bottom + 8;
  tooltip.style.left = `${Math.max(8, Math.min(innerWidth - width - 8, rect.left + (rect.width - width) / 2))}px`;
  tooltip.style.top = `${Math.max(8, Math.min(innerHeight - height - 8, top))}px`;
  const description = new Set((target.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
  description.add(tooltip.id);
  target.setAttribute('aria-describedby', [...description].join(' '));
}

const observer = new MutationObserver(() => {
  if (!target?.isConnected || !target.getClientRects().length || !target.dataset.tooltip) hide();
  else if (tooltip.matches(':popover-open') && tooltip.textContent !== target.dataset.tooltip) show();
});

function schedule(next, delay) {
  if (target === next) return;
  hide();
  if (!next?.dataset.tooltip) return;
  target = next;
  observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-tooltip', 'hidden', 'open', 'class'] });
  timer = setTimeout(show, delay);
}

document.addEventListener('pointerover', event => {
  if (event.pointerType === 'touch') return;
  schedule(event.target.closest('[data-tooltip]'), 400);
});
document.addEventListener('pointerout', event => {
  if (target?.contains(event.target) && !target.contains(event.relatedTarget)) hide();
});
document.addEventListener('focusin', event => {
  if (event.target.matches(':focus-visible')) schedule(event.target.closest('[data-tooltip]'), 0);
});
document.addEventListener('focusout', event => {
  if (target?.contains(event.target)) hide();
});
document.addEventListener('pointerdown', hide, true);
document.addEventListener('keydown', event => { if (event.key === 'Escape') hide(); });
document.addEventListener('scroll', hide, true);
window.addEventListener('resize', hide);
window.addEventListener('blur', hide);
