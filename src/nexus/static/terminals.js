import { t } from './i18n.js?v=145';
import { api } from './api.js?v=145';
import { icon } from './icons.js?v=145';
import { h, basename } from './views.js?v=145';
import { copyText } from './context-menu.js?v=145';
import './vendor/xterm.js?v=145';
import './vendor/xterm-addon-fit.js?v=145';

const $ = id => document.getElementById(id);
const terminalName = terminal => terminal.name.replace(/^终端 (\d+)$/, (_, number) => t('终端 {0}', number));

export class TerminalBrowser {
  constructor({ toast, onNavigate, onOpen, onOpenNavigation, onOpenThread }) {
    Object.assign(this, { toast, onNavigate, onOpen, onOpenThread });
    this.items = [];
    this.machines = [];
    this.instances = new Map();
    this.visible = false;
    $('terminals-open-nav').addEventListener('click', onOpenNavigation);
    $('terminals-refresh').addEventListener('click', () => this.refresh().catch(error => toast(error.message, true)));
    $('terminal-back').addEventListener('click', () => $('terminals-workspace').classList.remove('terminal-selected'));
    $('terminal-close').addEventListener('click', () => this.remove(this.selected));
    $('terminal-thread').addEventListener('click', () => {
      const terminal = this.items.find(item => item.id === this.selected);
      if (terminal?.threadId) onOpenThread(terminal.machineId, terminal.threadId);
    });
    $('terminal-screen').addEventListener('contextmenu', event => {
      event.preventDefault();
      const instance = this.instances.get(this.selected);
      if (instance) this.rightClick(instance);
    });
    $('terminals-list').addEventListener('click', event => {
      const close = event.target.closest('[data-close-terminal]');
      if (close) return this.remove(close.dataset.closeTerminal);
      const row = event.target.closest('[data-terminal-id]');
      if (row) this.select(row.dataset.terminalId);
    });
    $('terminal-machines').addEventListener('click', async event => {
      const button = event.target.closest('[data-terminal-machine]');
      if (!button) return;
      button.disabled = true;
      try { await this.create(button.dataset.terminalMachine); }
      catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    });
    $('terminal-keys').addEventListener('pointerdown', event => event.preventDefault());
    $('terminal-keys').addEventListener('click', event => {
      const button = event.target.closest('[data-terminal-key]');
      if (!button) return;
      if (button.dataset.terminalKey === 'ctrl') {
        this.ctrl = !this.ctrl;
        button.setAttribute('aria-pressed', this.ctrl);
      } else {
        const keys = { escape: '\x1b', tab: '\t', interrupt: '\x03', up: '\x1b[A', down: '\x1b[B', left: '\x1b[D', right: '\x1b[C' };
        this.send({ type: 'input', data: keys[button.dataset.terminalKey] });
      }
      this.instances.get(this.selected)?.term.focus();
    });
    new ResizeObserver(() => this.fit()).observe($('terminal-screen'));
    new MutationObserver(() => this.theme()).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-terminal-theme'] });
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => this.theme());
    window.visualViewport?.addEventListener('resize', () => this.fit());
    window.addEventListener('online', () => this.reconnect());
    document.addEventListener('visibilitychange', () => { if (!document.hidden) this.reconnect(); });
  }

  setMachines(machines) {
    this.machines = machines;
    this.render();
  }

  async refresh() {
    const { data } = await api('/terminals');
    this.items = data;
    for (const [id, instance] of this.instances) {
      if (!data.some(item => item.id === id)) {
        this.disconnect(id);
        instance.term.dispose();
        instance.host.remove();
        this.instances.delete(id);
      }
    }
    if (this.selected && !data.some(item => item.id === this.selected)) this.clear();
    this.render();
  }

  async open(id) {
    this.visible = true;
    await this.refresh();
    if (!id && innerWidth <= 720) {
      this.disconnect(this.selected);
      this.onNavigate(null);
      return;
    }
    const saved = localStorage.getItem('nexus-terminal');
    id = id || this.selected || (this.items.some(item => item.id === saved) ? saved : this.items[0]?.id);
    if (id && this.items.some(item => item.id === id)) this.select(id);
    else this.clear();
  }

  hide() {
    this.visible = false;
    this.disconnect(this.selected);
  }

  async create(machineId, cwd = '~', threadId) {
    const terminal = await api(`/machines/${encodeURIComponent(machineId)}/terminals`, {
      method: 'POST', body: { cwd, threadId },
    });
    return this.onOpen(terminal.id);
  }

  async remove(id) {
    if (!id) return;
    try {
      await api(`/terminals/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await this.refresh();
      if (!this.selected && this.items.length) this.select(this.items[0].id);
    } catch (error) { this.toast(error.message, true); }
  }

  handleEvent(event) {
    if (event.method.startsWith('nexus/terminal/')) this.refresh().catch(() => {});
  }

  render() {
    if (this.currentStatus) this.status(this.currentStatus.message, ...this.currentStatus.values);
    const name = id => this.machines.find(machine => machine.id === id)?.name || id;
    $('terminals-list').innerHTML = this.items.length ? this.items.map(terminal => `<div class="terminal-entry ${terminal.id === this.selected ? 'selected' : ''}"><button class="terminal-row" data-terminal-id="${h(terminal.id)}" data-tooltip="${h(terminal.cwd)}"><div class="session-title-row">${icon('terminal')}<span class="session-title">${h(terminalName(terminal))}</span><i class="status-dot ${terminal.status === 'running' ? 'connected' : ''}" aria-label="${terminal.status === 'running' ? t("运行中") : t("已退出")}"></i></div><div class="session-meta"><span class="session-context">${h(name(terminal.machineId))} · ${h(basename(terminal.cwd))}</span></div></button><button class="icon-button small terminal-remove" data-close-terminal="${h(terminal.id)}" aria-label="${t("关闭 {0}", h(terminalName(terminal)))}" data-tooltip="${t("关闭终端")}" data-i18n-data-tooltip="关闭终端">${icon('x')}</button></div>`).join('') : `<div class="list-empty" data-i18n="暂无终端">${t("暂无终端")}</div>`;
    $('terminal-machines').innerHTML = `<h2 class="session-status-heading" data-i18n="新建终端">${t("新建终端")}</h2>${this.machines.map(machine => `<button class="terminal-machine" data-terminal-machine="${h(machine.id)}" ${machine.demo ? 'disabled' : ''}>${icon('monitor')}<span>${h(machine.name)}</span>${icon('plus')}</button>`).join('')}`;
    const terminal = this.items.find(item => item.id === this.selected);
    if (!terminal) return;
    $('terminal-title').textContent = terminalName(terminal);
    $('terminal-location').textContent = `${name(terminal.machineId)} / ${terminal.cwd}`;
    $('terminal-location').dataset.tooltip = terminal.cwd;
    $('terminal-thread').hidden = !terminal.threadId;
    if (terminal.status === 'exited') this.status('已退出 · {0}', terminal.exitCode);
  }

  clear() {
    this.disconnect(this.selected);
    this.selected = null;
    $('terminal-view').hidden = true;
    $('terminal-empty').hidden = false;
    $('terminals-workspace').classList.remove('terminal-selected');
    if (this.visible) this.onNavigate(null);
  }

  select(id) {
    if (this.selected !== id) this.disconnect(this.selected);
    this.selected = id;
    localStorage.setItem('nexus-terminal', id);
    $('terminal-view').hidden = false;
    $('terminal-empty').hidden = true;
    $('terminals-workspace').classList.add('terminal-selected');
    let instance = this.instances.get(id);
    if (!instance) {
      const terminal = this.items.find(item => item.id === id);
      const host = document.createElement('div');
      host.className = 'terminal-instance';
      $('terminal-screen').append(host);
      const term = new window.Terminal({
        cols: terminal.cols, rows: terminal.rows, cursorBlink: true, scrollback: 10000,
        fontSize: 14, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
        allowProposedApi: false, disableStdin: true, rightClickSelectsWord: false,
      });
      const fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(host);
      term.textarea.setAttribute('aria-label', t("终端输入"));
      term.textarea.setAttribute('autocapitalize', 'off');
      instance = { term, fit, host, offset: 0 };
      this.instances.set(id, instance);
      term.onData(data => {
        if (!instance.live) return;
        if (this.ctrl && data.length === 1) {
          data = String.fromCharCode(data.toUpperCase().charCodeAt(0) & 31);
          this.ctrl = false;
          $('terminal-keys').querySelector('[data-terminal-key="ctrl"]').setAttribute('aria-pressed', 'false');
        }
        this.send({ type: 'input', data });
      });
      term.onBinary(data => {
        if (instance.live) this.send({ type: 'input-binary', data: btoa(data) });
      });
      term.onResize(({ cols, rows }) => { if (instance.live) this.send({ type: 'resize', cols, rows }); });
      term.attachCustomKeyEventHandler(event => {
        if (event.type === 'keydown' && (event.ctrlKey || event.metaKey)
          && event.key.toLowerCase() === 'c' && term.hasSelection()) return false;
        return true;
      });
    }
    for (const [key, value] of this.instances) value.host.hidden = key !== id;
    this.theme();
    this.render();
    this.onNavigate(id);
    this.connect(id);
    requestAnimationFrame(() => { this.fit(); instance.term.focus(); });
  }

  async rightClick(instance) {
    const { term } = instance;
    if (term.modes.mouseTrackingMode !== 'none') return;
    const selection = term.getSelection();
    try {
      if (selection) {
        await copyText(selection);
        term.clearSelection();
      } else if (instance.live) {
        const text = await navigator.clipboard.readText();
        if (!instance.live || instance !== this.instances.get(this.selected)) return;
        term.focus();
        term.paste(text);
      }
    } catch {
      this.toast(selection ? t("复制失败，请检查剪贴板权限") : t("无法读取剪贴板，请允许访问或使用粘贴快捷键"), true);
    }
  }

  theme() {
    const css = getComputedStyle($('terminal-screen'));
    const color = name => css.getPropertyValue(name).trim();
    for (const { term } of this.instances.values()) {
      term.options.theme = { background: color('--terminal-bg'), foreground: color('--terminal-text'), cursor: color('--terminal-text'),
        selectionBackground: color('--terminal-selection'), black: '#252525', red: '#c45b55', green: '#6c975f',
        yellow: '#b58b32', blue: '#5985c4', magenta: '#a76db7', cyan: '#429b9d', white: '#d4d4ce',
        brightBlack: '#85857c', brightRed: '#df8175', brightGreen: '#9abf81', brightYellow: '#d9b365',
        brightBlue: '#86a9dd', brightMagenta: '#c79bd4', brightCyan: '#80c3c3', brightWhite: '#f5f5ec' };
    }
  }

  status(message, ...values) {
    this.currentStatus = { message, values };
    $('terminal-status').textContent = t(message, ...values);
  }

  send(message) {
    const socket = this.instances.get(this.selected)?.socket;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }

  fit() {
    const instance = this.instances.get(this.selected);
    if (!this.visible || !instance || !$('terminal-screen').clientWidth || !$('terminal-screen').clientHeight) return;
    instance.fit.fit();
  }

  connect(id) {
    const instance = this.instances.get(id);
    if (!this.visible || !instance || instance.socket || instance.retry) return;
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${scheme}//${location.host}/api/terminals/${encodeURIComponent(id)}/stream?after=${instance.offset}`);
    instance.socket = socket;
    instance.live = false;
    this.status('连接中');
    socket.onmessage = event => {
      if (instance.socket !== socket) return;
      const message = JSON.parse(event.data);
      if (message.type === 'ready') {
        if (message.reset) instance.term.reset();
        instance.term.resize(message.cols, message.rows);
        instance.offset = message.offset;
        instance.replayEnd = message.end;
        if (message.offset === message.end) this.ready(instance);
      } else if (message.type === 'output') {
        const data = Uint8Array.from(atob(message.data), char => char.charCodeAt(0));
        instance.term.write(data, () => {
          instance.offset = message.offset;
          if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'ack', offset: message.offset }));
          if (!instance.live && instance.offset >= instance.replayEnd) this.ready(instance);
        });
      } else if (message.type === 'exit') {
        instance.exited = true;
        instance.live = false;
        instance.term.options.disableStdin = true;
        this.status('已退出 · {0}', message.exitCode);
        this.refresh().catch(() => {});
      }
    };
    socket.onclose = event => {
      if (instance.socket !== socket) return;
      instance.socket = null;
      instance.live = false;
      instance.term.options.disableStdin = true;
      if (event.code === 1008) { this.status('终端已关闭'); this.refresh().catch(() => {}); return; }
      if (!this.visible || this.selected !== id || instance.exited) return;
      this.status('重连中');
      instance.retry = setTimeout(() => { instance.retry = null; this.connect(id); }, 1500);
    };
  }

  ready(instance) {
    if (instance.exited) return;
    instance.live = true;
    instance.term.options.disableStdin = false;
    if (instance === this.instances.get(this.selected)) {
      this.status('已连接');
      this.fit();
      this.send({ type: 'resize', cols: instance.term.cols, rows: instance.term.rows });
    }
  }

  disconnect(id) {
    const instance = this.instances.get(id);
    if (!instance) return;
    clearTimeout(instance.retry);
    instance.retry = null;
    const socket = instance.socket;
    instance.socket = null;
    instance.live = false;
    socket?.close();
  }

  reconnect() {
    if (!this.visible || !this.selected) return;
    this.disconnect(this.selected);
    this.connect(this.selected);
    this.refresh().catch(() => {});
  }
}
