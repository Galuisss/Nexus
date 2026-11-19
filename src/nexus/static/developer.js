import { t } from './i18n.js?v=145';
import { api, threadPath } from './api.js?v=145';
import { icon } from './icons.js?v=145';
import { h } from './views.js?v=145';

const $ = id => document.getElementById(`developer-${id}`);

function toolUpdate(update, messageIndex, toolIndex) {
  const kind = { added: t("新增工具"), updated: t("更新工具"), unchanged: t("重新声明") }[update.kind];
  const diff = text => text.split('\n').map(line => {
    const style = line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : 'context';
    return `<span class="developer-diff-${style}">${h(line)}</span>`;
  }).join('\n');
  return `<section class="developer-tool-update"><header><code>${h(update.name)}</code><span class="developer-tool-kind">${kind}</span><button class="icon-button small" data-copy-developer="${messageIndex}" data-copy-tool="${toolIndex}" aria-label="${t("复制工具 {0} 的定义", h(update.name))}" data-tooltip="${t("复制工具定义")}" data-i18n-data-tooltip="复制工具定义">${icon('copy')}</button></header>${update.changes.map(change =>
    `<div class="developer-tool-change"><h4><code>${h(change.field)}</code><span>${{ added: t("新增"), updated: t("修改"), removed: t("移除") }[change.kind]}</span></h4><pre class="developer-diff">${diff(change.diff)}</pre></div>`
  ).join('')}<div class="developer-tool-definition"><h4 data-i18n="完整工具定义">${t("完整工具定义")}</h4><pre>${h(JSON.stringify(update.definition, null, 2))}</pre></div></section>`;
}

export class DeveloperPanel {
  constructor({ rightPanel, toast }) {
    Object.assign(this, { rightPanel, toast });
    this.activateSection('base');
    rightPanel.register('developer', $('pane'), () => this.show(false));
    $('close').addEventListener('click', () => this.show(false));
    $('refresh').addEventListener('click', () => this.refresh());
    $('tabs').addEventListener('click', event => {
      const tab = event.target.closest('[data-developer-section]');
      if (tab) this.activateSection(tab.dataset.developerSection);
    });
    $('tabs').addEventListener('keydown', event => {
      if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...$('tabs').children];
      const index = tabs.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + tabs.length) % tabs.length;
      this.activateSection(tabs[next].dataset.developerSection);
      tabs[next].focus();
    });
    $('pane').addEventListener('click', async event => {
      const button = event.target.closest('[data-copy-developer]');
      if (!button || !this.data) return;
      const message = this.data.runtimeMessages[Number(button.dataset.copyDeveloper)];
      const text = button.dataset.copyTool !== undefined
        ? JSON.stringify(message.toolUpdates[Number(button.dataset.copyTool)].definition, null, 2)
        : button.dataset.copyDeveloper === 'base' ? this.data.baseInstructions
        : button.dataset.copyDeveloper === 'custom' ? this.data.customInstructions : message.text;
      try {
        await navigator.clipboard.writeText(text);
        toast(button.dataset.copyTool !== undefined ? t("工具定义已复制") : t("指令已复制"));
      } catch (error) { toast(error.message, true); }
    });
  }

  get opened() { return !$('pane').hidden; }
  get key() { return this.selection && `${this.selection.machineId}/${this.selection.threadId}`; }
  get storageKey() { return `nexus-developer-panel:${this.key}`; }

  activateSection(name) {
    for (const tab of $('tabs').children) {
      const selected = tab.dataset.developerSection === name;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(tab.dataset.developerSection + '-section').hidden = !selected;
    }
  }

  select(selection) {
    this.selection = selection;
    this.generation = (this.generation || 0) + 1;
    this.data = null;
    this.signature = '';
    $('base').textContent = '';
    $('custom').textContent = '';
    $('copy-base').disabled = true;
    $('copy-custom').disabled = true;
    $('title').textContent = t("Developer 输入");
    $('runtime').replaceChildren();
    $('error').hidden = true;
    $('loading').hidden = true;
    $('refresh').disabled = false;
    $('sections').setAttribute('aria-busy', 'false');
    this.show(Boolean(selection && localStorage.getItem(this.storageKey) === 'true'));
  }

  show(open = true) {
    this.rightPanel.show('developer', open);
    document.querySelector('[data-thread-action="developer-inputs"]').setAttribute('aria-expanded', String(open));
    if (this.selection) localStorage.setItem(this.storageKey, String(open));
    if (open) this.refresh();
  }

  async refresh() {
    if (!this.opened || !this.selection) return;
    const selection = this.selection;
    const generation = ++this.generation;
    $('refresh').disabled = true;
    $('loading').hidden = Boolean(this.data);
    $('sections').setAttribute('aria-busy', 'true');
    try {
      const data = await api(`${threadPath(selection.machineId, selection.threadId)}/developer-inputs?preview=true`);
      if (generation !== this.generation) return;
      const signature = JSON.stringify(data);
      this.data = data;
      $('error').hidden = true;
      if (signature !== this.signature) {
        this.signature = signature;
        this.render();
      }
    } catch (error) {
      if (generation === this.generation) {
        $('error').textContent = error.message;
        $('error').hidden = false;
      }
    } finally {
      if (generation === this.generation) {
        $('loading').hidden = true;
        $('refresh').disabled = false;
        $('sections').setAttribute('aria-busy', 'false');
      }
    }
  }

  render() {
    const data = this.data;
    $('title').textContent = data.source === 'preview' ? t("Developer 输入（预期）") : t("Developer 输入");
    const base = data.baseInstructions || t("暂无已保存的模型基础指令");
    if ($('base').textContent !== base) $('base').textContent = base;
    $('copy-base').disabled = !data.baseInstructions;
    const custom = data.customInstructions || t("暂无自定义 developer 指令");
    if ($('custom').textContent !== custom) $('custom').textContent = custom;
    $('copy-custom').disabled = !data.customInstructions;
    const runtime = data.runtimeMessages.length ? data.runtimeMessages.map((message, index) =>
      `<article class="developer-message"><header><span>${t("消息 {0}", index + 1)}</span><button class="icon-button small" data-copy-developer="${index}" aria-label="${t("复制 developer 消息 {0}", index + 1)}" data-tooltip="${t("复制")}" data-i18n-data-tooltip="复制">${icon('copy')}</button></header><pre>${h(message.text)}</pre>${(message.toolUpdates || []).map((update, toolIndex) => toolUpdate(update, index, toolIndex)).join('')}</article>`
    ).join('') : `<div class="developer-empty" data-i18n="暂无已保存的运行时指令">${t("暂无已保存的运行时指令")}</div>`;
    if ($('runtime').innerHTML !== runtime) $('runtime').innerHTML = runtime;
  }

  handleEvent(event) {
    const params = event.params || {};
    if (event.method === 'nexus/resync'
      || event.method === 'nexus/machine/updated' && params.machine?.id === this.selection?.machineId && params.machine.status === 'connected'
      || event.machineId === this.selection?.machineId && (params.threadId || params.thread?.id) === this.selection?.threadId
        && (['turn/started', 'turn/completed', 'thread/settings/updated', 'thread/reverted'].includes(event.method)
          || this.data?.source === 'preview' && event.method === 'item/started')) {
      this.refresh();
    }
  }
}
