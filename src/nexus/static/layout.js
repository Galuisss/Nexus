export class RightPanel {
  constructor() {
    this.panels = new Map();
  }

  register(name, pane, close) {
    this.panels.set(name, { pane, close });
  }

  show(name, open) {
    if (open) {
      for (const [other, panel] of this.panels) {
        if (other !== name && !panel.pane.hidden) panel.close();
      }
    }
    this.panels.get(name).pane.hidden = !open;
    const active = [...this.panels.values()].find(panel => !panel.pane.hidden);
    const resizer = document.getElementById('side-resizer');
    resizer.hidden = !active;
    resizer.setAttribute('aria-controls', active?.pane.id || 'side-pane');
    document.getElementById('app').classList.toggle('side-open', Boolean(active));
  }
}

export function setupPaneResizing() {
  const app = document.getElementById('app');
  const handles = [...app.querySelectorAll('.pane-resizer')];
  const desktop = matchMedia('(min-width: 1081px)');
  const mobile = matchMedia('(max-width: 720px)');
  const minimum = { sidebar: 180, session: 220, detail: 360, side: 320 };
  const storageKey = 'nexus-pane-widths';
  let widths = {};
  let drag;
  try { widths = JSON.parse(localStorage.getItem(storageKey)) || {}; } catch { /* Use default widths. */ }

  const sideOpen = () => desktop.matches && app.classList.contains('side-open') && !app.classList.contains('files-view') && !app.classList.contains('terminals-view') && !app.classList.contains('groups-view');
  const paneWidth = name => app.querySelector({ sidebar: '.sidebar', session: app.classList.contains('files-view') ? '.files-browser' : app.classList.contains('terminals-view') ? '.terminals-browser' : app.classList.contains('groups-view') ? '.groups-browser' : '.session-pane', side: '.right-pane:not([hidden])' }[name])?.getBoundingClientRect().width || 0;
  const maximum = name => app.clientWidth - minimum.detail
    - (name === 'side' ? paneWidth('session') + paneWidth('sidebar') : (sideOpen() ? paneWidth('side') : 0) + (name === 'sidebar' ? paneWidth('session') : desktop.matches ? paneWidth('sidebar') : 0));
  const clamp = (width, min, max) => Math.max(min, Math.min(width, max));

  function render() {
    if (mobile.matches) return;
    const defaults = getComputedStyle(app);
    const available = app.clientWidth - minimum.detail - (sideOpen() ? minimum.side : 0);
    const sidebar = desktop.matches ? clamp(widths.sidebar ?? parseFloat(defaults.getPropertyValue('--sidebar-default')), minimum.sidebar, available - minimum.session) : 0;
    const session = clamp(widths.session ?? parseFloat(defaults.getPropertyValue('--session-default')), minimum.session, available - sidebar);
    app.style.setProperty('--sidebar-width', `${sidebar}px`);
    app.style.setProperty('--session-width', `${session}px`);
    const side = sideOpen() ? clamp(widths.side ?? 420, minimum.side, app.clientWidth - minimum.detail - sidebar - session) : 0;
    app.style.setProperty('--side-width', `${side}px`);
    for (const handle of handles) {
      const name = handle.dataset.pane;
      handle.setAttribute('aria-valuemin', minimum[name]);
      handle.setAttribute('aria-valuemax', Math.round(maximum(name)));
      handle.setAttribute('aria-valuenow', Math.round({ sidebar, session, side }[name]));
    }
  }

  function setWidth(name, width) {
    widths[name] = clamp(width, minimum[name], maximum(name));
    render();
  }

  function finishDrag() {
    if (!drag) return;
    drag.handle.classList.remove('dragging');
    document.body.classList.remove('resizing-panes');
    drag = null;
    localStorage.setItem(storageKey, JSON.stringify(widths));
  }

  for (const handle of handles) {
    const name = handle.dataset.pane;
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0 || drag) return;
      event.preventDefault();
      drag = { handle, pointerId: event.pointerId, x: event.clientX, width: paneWidth(name) };
      handle.setPointerCapture(event.pointerId);
      handle.focus({ preventScroll: true });
      handle.classList.add('dragging');
      document.body.classList.add('resizing-panes');
    });
    handle.addEventListener('pointermove', event => {
      if (drag?.handle === handle && drag.pointerId === event.pointerId) setWidth(name, drag.width + (event.clientX - drag.x) * (name === 'side' ? -1 : 1));
    });
    const release = event => {
      if (drag?.handle !== handle || drag.pointerId !== event.pointerId) return;
      handle.releasePointerCapture(event.pointerId);
      finishDrag();
    };
    handle.addEventListener('pointerup', release);
    handle.addEventListener('pointercancel', release);
    handle.addEventListener('lostpointercapture', finishDrag);
    handle.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const step = event.shiftKey ? 50 : 10;
      const width = event.key === 'Home' ? minimum[name] : event.key === 'End' ? maximum(name) : paneWidth(name) + (event.key === 'ArrowRight' ? step : -step) * (name === 'side' ? -1 : 1);
      setWidth(name, width);
      localStorage.setItem(storageKey, JSON.stringify(widths));
    });
  }
  window.addEventListener('resize', render);
  new MutationObserver(render).observe(app, { attributes: true, attributeFilter: ['class'] });
  render();
}
