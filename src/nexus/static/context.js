import { t, locale } from './i18n.js?v=145';
import { api, threadPath } from './api.js?v=145';
import { h } from './views.js?v=145';

const $ = id => document.getElementById(`context-${id}`);
const number = value => value == null ? '—' : value.toLocaleString(locale());
const percent = (tokens, total) => tokens == null || !total ? '—' : `${(tokens / total * 100).toFixed(2)}%`;
const colors = {
  base: '#bfa778', developer: '#98a3b8', custom: '#a093be',
  agents: '#79a58d', skills: '#6ca69b', user: '#d1a06e', assistant: '#6f95bb',
  tools: '#7ea4a8', tool_calls: '#9b99c6', tool_results: '#7394ac',
  reasoning: '#b18bb4', framing: '#c4a172', injected: '#be8d78',
  environment: '#99a68a', compaction: '#a69ab2', media: '#b7889d', output_delta: '#999993',
};

export class ContextPanel {
  constructor({ rightPanel, toast }) {
    Object.assign(this, { rightPanel, toast });
    rightPanel.register('context', $('pane'), () => this.show(false));
    $('close').addEventListener('click', () => this.show(false));
    $('refresh').addEventListener('click', () => this.refresh());
    $('export').addEventListener('click', () => this.export());
    const chart = $('donut').parentElement;
    chart.addEventListener('pointermove', event => {
      if (event.pointerType === 'mouse') this.showSegment(this.segmentAt(event), event);
    });
    chart.addEventListener('pointerleave', event => {
      if (event.pointerType === 'mouse') this.hideSegment();
    });
    chart.addEventListener('click', event => {
      if (event.pointerType === 'touch' || !matchMedia('(hover: hover)').matches) {
        const segment = this.segmentAt(event);
        if (segment?.id === this.segmentTooltip?.id) this.hideSegment();
        else this.showSegment(segment, event, true);
      }
    });
    document.addEventListener('pointerdown', event => {
      if (!chart.contains(event.target)) this.hideSegment();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') this.hideSegment();
    });
    $('pane').querySelector('.context-scroll').addEventListener('scroll', () => this.hideSegment());
    window.addEventListener('resize', () => this.hideSegment());
  }

  get opened() { return !$('pane').hidden; }
  get key() { return this.selection && `${this.selection.machineId}/${this.selection.threadId}`; }
  get storageKey() { return `nexus-context-panel:${this.key}`; }

  select(selection) {
    this.hideSegment();
    clearTimeout(this.timer);
    this.selection = selection;
    this.generation = (this.generation || 0) + 1;
    this.data = null;
    this.signature = '';
    $('title').textContent = t("上下文占比");
    $('used').textContent = '—';
    this.segments = [];
    $('segments').replaceChildren();
    $('categories').replaceChildren();
    $('error').hidden = true;
    $('export').disabled = true;
    this.show(Boolean(selection && localStorage.getItem(this.storageKey) === 'true'));
  }

  show(open = true) {
    this.rightPanel.show('context', open);
    document.querySelector('[data-thread-action="context-usage"]').setAttribute('aria-expanded', String(open));
    if (this.selection) localStorage.setItem(this.storageKey, String(open));
    if (open) this.refresh();
    else { clearTimeout(this.timer); this.hideSegment(); }
  }

  async refresh() {
    if (!this.opened || !this.selection) return;
    const generation = this.generation;
    if (this.request?.generation === generation) {
      this.request.pending = true;
      return;
    }
    const request = { generation };
    this.request = request;
    $('pane').setAttribute('aria-busy', 'true');
    try {
      const { machineId, threadId } = this.selection;
      const data = await api(`${threadPath(machineId, threadId)}/context-usage`);
      if (generation !== this.generation) return;
      const signature = JSON.stringify(data);
      this.data = data;
      if (signature !== this.signature) {
        this.signature = signature;
        this.render();
      }
      $('error').hidden = true;
      $('export').disabled = false;
    } catch (error) {
      if (generation === this.generation) {
        $('error').textContent = error.message;
        $('error').hidden = false;
      }
    } finally {
      if (this.request === request) {
        this.request = null;
        $('pane').setAttribute('aria-busy', 'false');
        if (request.pending) this.schedule();
      }
    }
  }

  schedule() {
    if (!this.opened) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refresh(), 400);
  }

  render() {
    const data = this.data;
    const used = data.usedTokens ?? data.knownTokens;
    $('title').textContent = data.source === 'preview' ? t("上下文占比（预期）") : t("上下文占比");
    $('used').textContent = number(used);
    $('donut').setAttribute('aria-label', t("已使用 {0} Token 的分类占比", number(used)));
    const categories = data.categories.filter(category => !['unused', 'output_delta'].includes(category.id));
    const other = data.categories.find(category => category.id === 'output_delta');
    if (other?.tokens) categories.push(other);
    const positive = categories.filter(category => category.tokens > 0);
    const occupied = positive.reduce((total, category) => total + category.tokens, 0);
    let offset = 0;
    this.segments = [];
    // Signed adjustments remain in the list; ring sectors show positive occupancy.
    $('segments').innerHTML = positive.map(category => {
      const size = category.tokens / occupied * 100;
      this.segments.push({ id: category.id, label: category.id === 'output_delta' ? t("其他") : t(category.label),
        start: offset, end: offset + size });
      const segment = `<circle data-context-segment="${category.id}" cx="108" cy="108" r="86" pathLength="100" stroke="${colors[category.id]}" stroke-dasharray="${size} ${100 - size}" stroke-dashoffset="${-offset}"/>`;
      offset += size;
      return segment;
    }).join('');
    if (this.segmentTooltip) {
      const { point, pinned, id } = this.segmentTooltip;
      this.showSegment(pinned ? this.segments.find(segment => segment.id === id) : this.segmentAt(point), point, pinned);
    }

    for (const category of categories) {
      let row = $('categories').querySelector(`[data-category="${category.id}"]`);
      if (!row) {
        row = document.createElement('li');
        row.className = 'context-category';
        row.dataset.category = category.id;
        row.style.setProperty('--category-color', colors[category.id]);
        row.innerHTML = `<span class="context-category-label"><i class="context-swatch"></i><span>${h(category.id === 'output_delta' ? t("其他") : t(category.label))}</span></span><span class="context-category-tokens"></span><span class="context-category-percent"></span>`;
        $('categories').append(row);
      }
      row.querySelector('.context-category-label > span').textContent = category.id === 'output_delta' ? t("其他") : t(category.label);
      row.classList.toggle('negative', category.tokens < 0);
      row.querySelector('.context-category-tokens').textContent = number(category.tokens);
      row.querySelector('.context-category-percent').textContent = percent(category.tokens, used);
    }
    for (const row of $('categories').children) {
      if (!categories.some(category => category.id === row.dataset.category)) row.remove();
    }
  }

  segmentAt(point) {
    const rect = $('donut').getBoundingClientRect();
    const x = (point.clientX - rect.left) / rect.width * 216 - 108;
    const y = (point.clientY - rect.top) / rect.height * 216 - 108;
    const radius = Math.hypot(x, y);
    if (radius < 75 || radius > 97) return null;
    const angle = ((Math.atan2(y, x) + Math.PI * 2.5) % (Math.PI * 2)) / (Math.PI * 2) * 100;
    return this.segments?.find(segment => angle >= segment.start && angle < segment.end);
  }

  showSegment(segment, point, pinned = false) {
    if (!segment) { this.hideSegment(); return; }
    const tooltip = $('segment-tooltip');
    tooltip.textContent = segment.label;
    if (!tooltip.matches(':popover-open')) tooltip.showPopover();
    const { width, height } = tooltip.getBoundingClientRect();
    const left = pinned ? point.clientX - width / 2 : point.clientX + 12;
    const top = pinned ? point.clientY - height - 14 : point.clientY + 16;
    tooltip.style.left = `${Math.max(8, Math.min(innerWidth - width - 8, left))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(innerHeight - height - 8, top))}px`;
    $('donut').setAttribute('aria-describedby', tooltip.id);
    this.segmentTooltip = { id: segment.id, pinned, point: { clientX: point.clientX, clientY: point.clientY } };
  }

  hideSegment() {
    $('segment-tooltip').hidePopover();
    $('donut').removeAttribute('aria-describedby');
    this.segmentTooltip = null;
  }

  export() {
    if (!this.data) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(this.data, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `context-${this.selection.threadId}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    this.toast(t("上下文数据已导出"));
  }

  handleEvent(event) {
    const params = event.params || {};
    if (event.method === 'nexus/resync'
      || event.method === 'nexus/machine/updated' && params.machine?.id === this.selection?.machineId && params.machine.status === 'connected'
      || event.machineId === this.selection?.machineId && (params.threadId || params.thread?.id) === this.selection?.threadId
        && (['thread/tokenUsage/updated', 'turn/completed', 'thread/reverted', 'thread/settings/updated'].includes(event.method)
          || event.method === 'item/completed' && params.item.type === 'contextCompaction')) this.schedule();
  }
}
