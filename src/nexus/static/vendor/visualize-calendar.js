// Nexus day calendar. SPDX-License-Identifier: Apache-2.0
(() => {
  if (customElements.get('viz-calendar')) return;

  const sheet = `
    :host { display: block; min-width: 0; color: var(--foreground); font: inherit; }
    * { box-sizing: border-box; }
    .calendar { display: grid; grid-template-columns: 108px minmax(0, 1fr); gap: 20px; }
    .date { display: flex; flex-direction: column; gap: 6px; }
    .day { font-size: 40px; line-height: 1.15; font-weight: 600; }
    .weekday, .zone { color: var(--muted-foreground); }
    .zone { overflow-wrap: anywhere; font-size: 12px; }
    .timeline { position: relative; margin-top: 10px; min-width: 0; }
    .tick { position: absolute; left: 0; right: 0; border-top: 1px solid var(--border); margin-left: 48px; }
    .tick span { position: absolute; right: calc(100% + 10px); transform: translateY(-50%); color: var(--muted-foreground); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .entries { position: absolute; inset: 0 0 0 48px; }
    .event { position: absolute; padding: 0 4px 4px 0; }
    .content { display: flex; width: 100%; height: 100%; flex-direction: column; gap: 3px; justify-content: center; padding: 7px 10px; border: 0; border-left: 3px solid var(--tone); border-radius: 6px; background: color-mix(in srgb, var(--tone) 15%, var(--background)); color: var(--foreground); font: inherit; text-align: left; overflow: hidden; }
    button.content { cursor: pointer; }
    button.content:hover { background: color-mix(in srgb, var(--tone) 23%, var(--background)); }
    button.content:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
    .title, .detail { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: none; }
    .title { font-weight: 600; }
    .video { float: right; width: 16px; height: 16px; margin-left: 6px; }
    .detail { font-size: 12px; color: var(--muted-foreground); }
    .event { container-type: size; }
    @container (max-height: 52px) { .detail { display: none; } .content { padding-block: 3px; } }
    .past { opacity: .65; }
    .now { position: absolute; left: 48px; right: 0; border-top: 2px solid var(--red); pointer-events: none; }
    .now::before { content: ''; position: absolute; left: -4px; top: -5px; width: 8px; height: 8px; border-radius: 50%; background: var(--red); }
    .now span { position: absolute; right: calc(100% + 10px); transform: translateY(-50%); background: var(--background); color: var(--red); font-size: 12px; white-space: nowrap; }
    .empty { margin: 16px 0 0 60px; color: var(--muted-foreground); }
    :host { container-type: inline-size; }
    @container (max-width: 480px) {
      .calendar { grid-template-columns: minmax(0, 1fr); gap: 12px; }
      .date { flex-direction: row; align-items: baseline; gap: 10px; }
      .day { font-size: 28px; }
      .content { padding-inline: 7px; }
    }
  `;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const minutes = value => {
    const match = /^(\d{2}):(\d{2})$/.exec(value);
    if (!match || Number(match[2]) > 59 || Number(match[1]) > 24 || (match[1] === '24' && match[2] !== '00')) {
      throw new Error('时间请使用 00:00 至 24:00 范围内的 HH:MM。');
    }
    return Number(match[1]) * 60 + Number(match[2]);
  };

  function arrange(events) {
    const ordered = [...events].sort((a, b) => a.from - b.from || a.until - b.until);
    let batch = [], boundary = -1;
    const layout = () => {
      const occupied = [];
      for (const item of batch) {
        let column = 0;
        while (occupied[column] > item.from) column++;
        item.column = column;
        occupied[column] = item.until;
      }
      for (const item of batch) item.columns = occupied.length;
    };
    for (const item of ordered) {
      if (item.from >= boundary) {
        layout();
        batch = [];
      }
      batch.push(item);
      boundary = Math.max(boundary, item.until);
    }
    layout();
    return ordered;
  }

  class DayCalendar extends HTMLElement {
    static observedAttributes = ['date', 'start', 'end', 'now', 'events', 'lang', 'time-zone', 'empty-label', 'interactive'];
    #events;
    #pointer = matchMedia('(pointer: coarse)');

    constructor() {
      super();
      if (Object.hasOwn(this, 'events')) {
        const value = this.events;
        delete this.events;
        this.#events = value;
      }
    }
    get events() { return this.#events === undefined ? JSON.parse(this.getAttribute('events') || '[]') : this.#events; }
    set events(value) { this.#events = value; if (this.isConnected) this.render(); }
    connectedCallback() {
      this.classList.add('widget');
      if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
      this.#pointer.addEventListener('change', this.render);
      this.render();
    }
    disconnectedCallback() { this.#pointer.removeEventListener('change', this.render); }
    attributeChangedCallback() { if (this.isConnected && this.shadowRoot) this.render(); }

    render = () => {
      const style = element('style', '');
      style.textContent = sheet;
      try {
        const dateValue = this.getAttribute('date') || '';
        const date = new Date(`${dateValue}T00:00:00Z`);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(dateValue) || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== dateValue) {
          throw new Error('请提供 YYYY-MM-DD 格式的有效日期。');
        }
        const begin = minutes(this.getAttribute('start') || '09:00');
        const end = minutes(this.getAttribute('end') || '17:00');
        if (end <= begin) throw new Error('结束时间须晚于开始时间。');
        const now = this.hasAttribute('now') ? minutes(this.getAttribute('now')) : null;
        const input = this.events;
        if (!Array.isArray(input)) throw new Error('日程须为事件数组。');
        const events = arrange(input.map((entry, index) => {
          const from = minutes(entry.start), until = minutes(entry.end);
          if (until <= from) throw new Error('事件结束时间须晚于开始时间。');
          return { entry, index, from: Math.max(begin, from), until: Math.min(end, until) };
        }).filter(item => item.from < item.until));
        const interactive = this.hasAttribute('interactive');
        const minimum = interactive && this.#pointer.matches ? 48 : 32;
        const hour = Math.max(64, ...events.map(item => minimum * 60 / (item.until - item.from)));
        const duration = end - begin;
        const locale = this.getAttribute('lang') || document.documentElement.lang || navigator.language;
        const format = options => new Intl.DateTimeFormat(locale, { ...options, timeZone: 'UTC' }).format(date);
        const calendar = element('section', 'calendar');
        calendar.setAttribute('aria-label', format({ dateStyle: 'full' }));
        const heading = element('header', 'date');
        heading.append(element('span', 'weekday', format({ weekday: 'short' })), element('span', 'day', new Intl.NumberFormat(locale).format(date.getUTCDate())));
        const zone = this.getAttribute('time-zone');
        if (zone) heading.append(element('span', 'zone', zone));
        const timeline = element('div', 'timeline');
        timeline.style.height = `${duration / 60 * hour + 12}px`;
        const tick = time => {
          const node = element('div', 'tick');
          node.style.top = `${(time - begin) / 60 * hour}px`;
          node.append(element('span', '', `${String(Math.floor(time / 60)).padStart(2, '0')}:${String(time % 60).padStart(2, '0')}`));
          timeline.append(node);
        };
        tick(begin);
        for (let time = Math.floor(begin / 60 + 1) * 60; time < end; time += 60) {
          if (time - begin >= 30 && end - time >= 30) tick(time);
        }
        tick(end);
        const entries = element('div', 'entries');
        for (const item of events) {
          const { entry, index, from, until, column, columns } = item;
          const wrapper = element('div', `event${now !== null && until <= now ? ' past' : ''}`);
          wrapper.style.top = `${(from - begin) / 60 * hour}px`;
          wrapper.style.height = `${(until - from) / 60 * hour}px`;
          wrapper.style.left = `${column / columns * 100}%`;
          wrapper.style.width = `${100 / columns}%`;
          const tone = ['blue', 'green', 'red', 'orange', 'purple', 'yellow'].includes(entry.tone) ? entry.tone : 'blue';
          wrapper.style.setProperty('--tone', `var(--${tone})`);
          const body = element(interactive ? 'button' : 'div', 'content');
          const label = `${entry.title}, ${entry.start}–${entry.end}${entry.detail ? `, ${entry.detail}` : ''}`;
          body.setAttribute('aria-label', label);
          body.dataset.tooltip = label;
          const title = element('span', 'title');
          if (entry.video) title.innerHTML = '<svg class="video" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="3" y="6" width="12" height="12" rx="2"/><path d="m15 10 6-4v12l-6-4"/></svg>';
          title.append(document.createTextNode(entry.title));
          body.append(title);
          if (entry.detail) body.append(element('span', 'detail', entry.detail));
          if (interactive) {
            body.type = 'button';
            body.onclick = () => this.dispatchEvent(new CustomEvent('eventselect', { detail: { event: input[index], index }, bubbles: true, composed: true }));
          }
          wrapper.append(body);
          entries.append(wrapper);
        }
        timeline.append(entries);
        if (!events.length) timeline.append(element('p', 'empty', this.getAttribute('empty-label') || '暂无日程'));
        if (now !== null && now >= begin && now <= end) {
          const marker = element('div', 'now');
          marker.style.top = `${(now - begin) / 60 * hour}px`;
          marker.append(element('span', '', this.getAttribute('now')));
          timeline.append(marker);
        }
        calendar.append(heading, timeline);
        this.shadowRoot.replaceChildren(style, calendar);
      } catch (error) {
        const message = element('p', 'error', error.message);
        message.role = 'alert';
        this.shadowRoot.replaceChildren(style, message);
      }
    };
  }
  customElements.define('viz-calendar', DayCalendar);
})();
