import { t, locale } from './i18n.js?v=145';
import { api, threadPath } from './api.js?v=145';
import { icon } from './icons.js?v=145';
import { ConversationRenderer } from './conversation.js?v=145';
import { ConversationController } from './conversation-controller.js?v=145';
import { ConversationActions, draftStorageKey, setQuestionBusy, bindQuestionInputs, renderRequestCards } from './conversation-actions.js?v=145';
import * as view from './views.js?v=145';
import * as composer from './composer.js?v=145';
import { GoalPanel } from './goals.js?v=145';
import { enhanceSelects, closeSelectMenu, updateSelectOptions, setSettingsDisabled } from './select.js?v=145';
import { toolCompactControl } from './features.js?v=145';

const $ = id => document.getElementById(`side-${id}`);

export class SidePanel {
  constructor({ toast, models, threads, connection, openThread, openImage, refreshRequests, rightPanel, openModal, closeModal }) {
    Object.assign(this, { toast, models, threads, connection, openThread, openImage, refreshRequests, rightPanel });
    rightPanel.register('side', $('pane'), () => this.show(false));
    this.renderer = new ConversationRenderer($('conversation'));
    this.contextRenderer = new ConversationRenderer($('context'));
    this.conversation = new ConversationController({
      onSnapshot: result => {
        this.thread = result.thread;
        this.sharedContext = result.thread.sideConversation?.sharedContext || [];
        this.settings = result.settings;
        this.tokenUsage = result.tokenUsage ?? this.tokenUsage;
        this.names.set(this.id, result.thread.name);
        this.error();
      },
      onEvent: (event, turnChanged) => this.applyEvent(event, turnChanged),
      onChange: ({ initial }) => this.render(initial),
      onError: error => this.error(error.message),
    });
    this.sharedContext = [];
    this.goalPanel = new GoalPanel({
      element: $('goal-panel'), button: $('goal'), conversation: this.conversation, openModal, closeModal,
    });
    this.tokenUsage = null;
    this.ids = [];
    this.names = new Map();
    this.requests = [];
    this.actions = new ConversationActions({
      conversation: this.conversation, state: this, input: $('input'), transcript: $('conversation'),
      thread: () => this.thread,
      onChange: bottom => bottom ? this.resize(true) : this.render(),
      onCommit: context => { if (context.selection === this.conversation.selection) this.error(); },
      onSettings: result => { this.settings = result.settings; },
      refreshRequests,
    });
    this.bindEvents();
  }

  get path() { return threadPath(this.parent.machineId, this.id); }
  get key() { return this.parent && `${this.parent.machineId}/${this.parent.threadId}`; }
  get running() { return view.isRunning(this.thread) || this.conversation.turns.some(view.isTurnRunning); }
  get storageKey() { return `nexus-side-panel:${this.key}`; }
  draftKey(id = this.id) { return draftStorageKey(`${this.parent.machineId}/${id}`); }
  error(message = '') { $('error').hidden = !message; $('error').textContent = message; }
  run(fn) { return (...args) => Promise.resolve(fn(...args)).catch(error => this.error(error.message)); }

  saveDraft() {
    this.actions.saveDraft();
  }

  remember() {
    if (this.parent) localStorage.setItem(this.storageKey, JSON.stringify({ open: this.opened, id: this.id }));
  }

  show(open = true) {
    this.opened = open;
    this.rightPanel.show('side', open);
    document.getElementById('toggle-side').classList.toggle('active', open);
    document.getElementById('toggle-side').setAttribute('aria-expanded', String(open));
    this.remember();
    if (!open) closeSelectMenu();
  }

  async setParent(parent) {
    this.saveDraft();
    this.conversation.select(null);
    this.id = null;
    this.parent = parent;
    this.ids = [];
    this.sharedContext = [];
    this.tokenUsage = null;
    this.thread = null;
    this.editing = null;
    this.opening = null;
    this.creating = false;
    this.deleting = null;
    this.error();
    if (!parent) { this.show(false); return; }
    let saved;
    try { saved = JSON.parse(localStorage.getItem(this.storageKey)); } catch { /* Closed by default. */ }
    this.opened = Boolean(saved?.open);
    this.show(this.opened);
    this.render();
    const key = this.key;
    await this.loadList();
    if (key !== this.key || this.opening) return;
    if (this.opened && this.ids.length) await this.select(this.ids.includes(saved.id) ? saved.id : this.ids.at(-1));
    else this.show(false);
  }

  async loadList() {
    if (!this.parent) return;
    const key = this.key;
    const opening = this.opening;
    const result = await api(`${threadPath(this.parent.machineId, this.parent.threadId)}/sides`);
    if (key !== this.key || opening !== this.opening) return;
    this.ids = result.data;
    for (const [id, name] of Object.entries(result.names || {})) this.names.set(id, name);
    if (this.id && !this.ids.includes(this.id)) await this.remove(this.id);
    this.renderTabs();
  }

  async deleteCurrent() {
    if (!this.id || this.deleting) return;
    const key = this.key;
    const id = this.id;
    const attachmentKey = this.actions.key;
    const draftKey = this.draftKey(id);
    const parentPath = threadPath(this.parent.machineId, this.parent.threadId);
    this.deleting = id;
    this.error();
    this.render();
    try {
      await api(`${parentPath}/sides/${encodeURIComponent(id)}`, { method: 'DELETE' });
      localStorage.removeItem(draftKey);
      this.attachments.delete(attachmentKey);
      if (key === this.key) await this.remove(id);
      this.toast(t("侧聊已删除"));
    } finally {
      if (this.deleting === id) this.deleting = null;
      this.render();
    }
  }

  async remove(id) {
    const index = this.ids.indexOf(id);
    if (index < 0 && this.id !== id) return;
    this.ids = this.ids.filter(candidate => candidate !== id);
    this.names.delete(id);
    this.statuses?.delete(id);
    this.attachments.delete(`${this.parent.machineId}/${id}`);
    localStorage.removeItem(this.draftKey(id));
    if (this.id === id) {
      this.id = null;
      this.conversation.select(null);
      this.sharedContext = [];
      this.tokenUsage = null;
      this.thread = null;
      this.settings = {};
      this.editing = null;
      $('input').value = '';
      this.remember();
      if (this.ids.length) await this.select(this.ids[Math.max(0, Math.min(index, this.ids.length - 1))]);
      else { this.show(false); document.getElementById('message-input').focus(); }
    }
    this.render();
  }

  async open({ fresh = false, text = '', attachments = [] } = {}) {
    if (!this.parent || this.creating) return false;
    const parent = this.parent;
    const selectedId = this.id;
    const hasMessage = Boolean(text || attachments.length);
    const opening = {};
    this.opening = opening;
    if (hasMessage) {
      const body = { text, actor: 'human', clientUserMessageId: crypto.randomUUID().replaceAll('-', '') };
      if (attachments.length) body.input = [...(text ? [{ type: 'text', text }] : []), ...attachments.map(file => file.input)];
      opening.submission = {
        body,
        item: { id: body.clientUserMessageId, type: 'userMessage', content: body.input || [{ type: 'text', text }], startedAtMs: Date.now(), _local: true },
      };
      this.saveDraft();
      this.conversation.select(null);
      this.id = null;
      this.sharedContext = [];
      this.thread = null;
      this.settings = {};
      this.tokenUsage = null;
      this.editing = null;
      $('input').value = '';
      this.creating = true;
    }
    this.show();
    this.error();
    if (hasMessage) this.render(true);
    try {
      await this.loadList();
      if (this.opening !== opening || !this.opened) return false;
      let id = selectedId && this.ids.includes(selectedId) ? selectedId : this.ids.at(-1);
      if (fresh || !id) {
        this.creating = true;
        this.render();
        const result = await api(`${threadPath(parent.machineId, parent.threadId)}/sides`, { method: 'POST' });
        id = result.thread.id;
        if (this.opening === opening) {
          if (!this.ids.includes(id)) this.ids.push(id);
          this.names.set(id, result.thread.name);
        }
      }
      if (hasMessage) {
        // Delivery belongs to the created side even after the user navigates away.
        const path = threadPath(parent.machineId, id);
        const submission = opening.submission;
        Object.assign(submission, { path, key: `${parent.machineId}/${id}` });
        this.conversation.submissions.set(submission.item.id, submission);
        const delivery = api(`${path}/messages`, { method: 'POST', body: submission.body });
        if (this.opening === opening && this.opened) {
          const refresh = this.select(id);
          submission.selection = this.conversation.selection;
          refresh.then(() => {
            if (submission.selection === this.conversation.selection && this.opened) $('input').focus();
          }).catch(error => {
            if (submission.selection === this.conversation.selection) this.error(error.message);
          });
        }
        try {
          const result = await delivery;
          submission.turnId = result.turnId || result.turn?.id;
          await this.actions.committed(submission, result);
        } catch (error) {
          submission.item = { ...submission.item, _sendError: error.message };
          if (submission.selection === this.conversation.selection) this.render();
          throw error;
        }
        return true;
      }
      if (this.opening !== opening || !this.opened) return true;
      await this.select(id);
      if (this.opening === opening) $('input').focus();
      return true;
    } catch (error) {
      if (this.opening === opening) this.error(error.message);
      throw error;
    } finally { if (this.opening === opening) { this.creating = false; this.render(); } }
  }

  async select(id) {
    this.saveDraft();
    closeSelectMenu();
    $('shared').open = false;
    this.id = id;
    this.conversation.select({ machineId: this.parent.machineId, threadId: id });
    this.sharedContext = [];
    this.settings = {};
    this.tokenUsage = null;
    this.thread = null;
    this.editing = null;
    this.error();
    this.show();
    $('input').value = localStorage.getItem(this.draftKey()) || '';
    this.render(true);
    await this.refresh();
  }

  refresh(options) { return this.conversation.refresh(options); }

  async fork(params) {
    if (this.creating || this.sending || this.deleting || this.editing?.saving) return;
    const key = this.key;
    this.creating = true;
    this.render();
    try {
      const result = await api(`${this.path}/fork`, { method: 'POST', body: params, timeout: 60_000 });
      if (key !== this.key) return;
      const id = result.thread.id;
      if (!this.ids.includes(id)) this.ids.push(id);
      this.names.set(id, result.thread.name);
      await this.select(id);
      this.toast(t("已创建分叉侧聊"));
    } finally {
      if (key === this.key) { this.creating = false; this.render(); }
    }
  }

  async older() {
    const height = $('scroll').scrollHeight;
    $('older').disabled = true;
    try {
      if (await this.conversation.older()) requestAnimationFrame(() => { $('scroll').scrollTop += $('scroll').scrollHeight - height; });
    } finally { $('older').disabled = false; }
  }

  handleEvent(event) {
    if (!this.parent) return;
    this.conversation.handleEvent(event);
    const p = event.params || {};
    if (event.machineId !== this.parent.machineId) return;
    if ((event.method === 'nexus/side/deleted' && p.parentThreadId === this.parent.threadId)
        || (event.method === 'thread/deleted' && this.ids.includes(p.threadId))) {
      this.run(() => this.remove(p.threadId))();
      return;
    }
    if (event.method === 'nexus/side/created' && p.parentThreadId === this.parent.threadId) this.run(() => this.loadList())();
    if (event.method === 'thread/status/changed' && this.ids.includes(p.threadId)) {
      this.statuses ||= new Map();
      this.statuses.set(p.threadId, p.status);
      this.renderTabs();
    }
  }

  applyEvent(event, turnChanged) {
    const p = event.params || {};
    const method = event.method;
    if (method === 'thread/settings/updated') {
      const settings = { ...p.threadSettings, ...p.settings };
      if ('effort' in settings) settings.reasoningEffort = settings.effort;
      this.settings = { ...this.settings, ...settings };
    }
    else if (method === 'thread/tokenUsage/updated') this.tokenUsage = p.tokenUsage;
    else if (method === 'thread/reverted') this.tokenUsage = null;
    else if (method === 'thread/status/changed' && this.thread) this.thread.status = p.status;
    else if (method === 'thread/name/updated') this.names.set(this.id, p.threadName || p.name);
    else if (method === 'error') this.error(p.error?.message || p.message || t("执行出错"));
    else if (turnChanged) {
      this.renderer.invalidate(p.turnId || p.turn?.id, p.itemId || p.item?.id);
      if (this.thread && ['turn/started', 'turn/completed'].includes(method)) this.thread.status = { type: method === 'turn/started' ? 'active' : 'idle' };
    }
    this.scheduleRender();
  }

  scheduleRender() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = null; this.render(); });
  }

  renderTabs() {
    const tabs = $('tabs');
    const signature = JSON.stringify([locale(), this.ids, this.id, this.ids.map(id => [this.names.get(id), this.statuses?.get(id)?.type])]);
    if (signature === this.tabsSignature) return;
    this.tabsSignature = signature;
    const left = tabs.scrollLeft;
    tabs.innerHTML = this.ids.map((id, index) => `<button type="button" role="tab" id="side-tab-${view.h(id)}" aria-controls="side-scroll" data-side-id="${view.h(id)}" aria-selected="${id === this.id}" tabindex="${id === this.id ? 0 : -1}" class="side-tab ${id === this.id ? 'active' : ''}">${this.statuses?.get(id)?.type === 'active' ? '<i class="working-pulse"></i>' : ''}<span>${view.h(this.names.get(id) || t("侧聊 {0}", index + 1))}</span></button>`).join('');
    if (this.id) $('scroll').setAttribute('aria-labelledby', `side-tab-${this.id}`);
    tabs.scrollLeft = left;
  }

  render(bottom = false) {
    const scroll = $('scroll');
    const nearBottom = bottom || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;
    const pending = !this.id && this.opening?.submission;
    const turns = pending ? [{ id: `local:${pending.item.id}`, status: 'pending', items: [pending.item], _local: true }] : this.conversation.visibleTurns;
    this.renderTabs();
    $('shared').hidden = this.conversation.loading || !this.sharedContext.length;
    $('boundary').hidden = this.conversation.loading || !this.sharedContext.length;
    this.contextRenderer.render(this.sharedContext, this.threads(), { machineId: this.parent?.machineId, cwd: this.thread?.cwd, readOnly: true });
    const contextKey = JSON.stringify([this.id, this.sharedContext.map(turn => [turn.id, turn.items.length, turn.items.at(-1)?.id])]);
    if (contextKey !== this.contextKey) {
      this.contextKey = contextKey;
      requestAnimationFrame(() => { $('context').scrollTop = $('context').scrollHeight; });
    }
    if (!this.editing?.composing && !$('conversation').querySelector('[data-composing]')) {
      this.renderer.render(turns, this.threads(), { settings: this.settings, machineId: this.parent?.machineId, cwd: this.thread?.cwd, running: this.running, busy: this.sending || this.creating || Boolean(this.deleting), editing: this.editing });
    }
    $('loading').hidden = Boolean(turns.length) || (!this.conversation.loading && !this.creating);
    this.goalPanel.render({ running: this.running, disabled: !this.id || this.conversation.loading || this.sending || Boolean(this.deleting) });
    $('older').hidden = !this.conversation.cursor || this.conversation.loading;
    const connected = this.connection(this.parent?.machineId);
    $('status').textContent = connected || (this.thread ? view.statusLabel(this.thread) : '');
    $('status').className = `status-label ${this.running ? 'running' : ''}`;
    $('new').disabled = this.creating || this.sending || Boolean(this.deleting);
    $('delete').disabled = !this.id || this.conversation.loading || this.creating || this.sending || this.uploadingFiles || Boolean(this.deleting) || this.editing?.saving;
    $('input').disabled = !this.id || this.conversation.loading || Boolean(this.deleting);
    const attachments = this.attachments.get(this.actions.key) || [];
    $('send').disabled = !this.id || this.creating || this.sending || this.uploadingFiles || this.conversation.loading || Boolean(this.deleting) || this.editing?.saving || (!$('input').value.trim() && !attachments.length);
    $('attach').disabled = !this.id || this.uploadingFiles || this.sending || Boolean(this.deleting);
    $('interrupt').hidden = !this.running;
    $('interrupt').disabled = Boolean(this.deleting);
    const goalControls = this.conversation.goal && this.conversation.goal.status !== 'complete';
    const resumable = !goalControls && !this.running && this.conversation.turns.at(-1)?.status === 'interrupted';
    $('working-interrupt').hidden = !this.running;
    $('working-interrupt').disabled = Boolean(this.deleting);
    for (const id of ['resume', 'working-resume']) {
      $(id).hidden = !resumable;
      $(id).disabled = this.sending || this.editing?.saving || this.conversation.loading || Boolean(this.deleting);
    }
    $('working').hidden = (!this.running && !resumable) || this.conversation.loading;
    $('working').classList.toggle('resumable', resumable);
    this.tick();
    this.renderControls();
    this.renderRequests();
    $('conversation').querySelectorAll('[data-question-item]').forEach(form => setQuestionBusy(form, this.sending || this.editing?.saving));
    $('attachments').hidden = !attachments.length;
    $('attachments').innerHTML = attachments.map((file, index) => `<div class="attachment-chip">${file.preview ? `<button type="button" class="attachment-preview" data-side-preview="${index}" aria-label="${t("预览 {0}", view.h(file.name))}"><img src="${view.h(file.preview)}" alt="${view.h(file.name)}"></button>` : icon('file-code')}<span>${view.h(file.name)}</span><button type="button" data-side-remove="${index}" aria-label="${t("移除附件")}" data-i18n-aria-label="移除附件">${icon('x')}</button></div>`).join('');
    if (nearBottom && !this.editing) requestAnimationFrame(() => { scroll.scrollTop = scroll.scrollHeight; });
  }

  tick() {
    this.renderer.tick();
    const turn = [...this.conversation.turns].reverse().find(view.isTurnRunning);
    const duration = turn && view.turnDuration(turn);
    $('duration').textContent = duration == null ? '' : view.formatDuration(duration);
    const item = [...(turn?.items || [])].reverse().find(item => item._active ?? (item.status === 'inProgress' || view.isCompactionRunning(item, true)));
    $('activity').textContent = this.thread && view.isWaiting(this.thread) ? view.statusLabel(this.thread) : item?.type === 'contextCompaction' ? t("正在压缩") : item?.type === 'toolCallCompaction' ? t("正在压缩 Tool call") : item?.type === 'agentMessage' ? t("正在回复") : item?.type === 'commandExecution' ? t("执行命令") : item?.progressMessage || t("思考中");
  }

  renderControls() {
    const settings = this.settings || {};
    const models = this.models();
    updateSelectOptions($('model'), composer.modelOptions(models, settings.model));
    const ultra = settings.reasoningEffort === 'ultra';
    updateSelectOptions($('effort'), (composer.supportsUltra(models, settings.model) ? `<option value="ultra" ${ultra ? 'selected' : ''}>Ultra</option>` : '')
      + composer.effortOptions(models, settings.model, ultra ? '' : settings.reasoningEffort));
    if (ultra) $('effort').value = 'ultra';
    enhanceSelects($('pane'));
    setSettingsDisabled($('model'), !this.id || this.conversation.loading || Boolean(this.deleting), this.settingsSaving);
    setSettingsDisabled($('effort'), !this.id || this.conversation.loading || Boolean(this.deleting), this.settingsSaving);
    composer.renderSpeedControl($('fast'), models, settings.model, settings.serviceTier);
    setSettingsDisabled($('fast'), !this.id || Boolean(this.deleting), this.settingsSaving);
    toolCompactControl($('tool-compact'), settings, !this.id || Boolean(this.deleting), this.settingsSaving);
    const contextSignature = JSON.stringify([locale(), this.tokenUsage]);
    if (contextSignature !== this.contextSignature) {
      this.contextSignature = contextSignature;
      const context = composer.contextInfo(this.tokenUsage);
      $('context-percent').textContent = context ? `${context.percent}%` : '—';
      $('context-ring').style.setProperty('--context-used', `${context?.percent || 0}%`);
      $('context-ring').classList.toggle('unknown', !context);
      $('context-popover').innerHTML = composer.contextContents(this.tokenUsage);
    }
  }

  patch(settings) { return this.actions.patchSettings(settings); }

  async send(options) {
    if (this.creating || this.deleting) return;
    if (/^\/(?:side|btw)(?:\s|$)/i.test($('input').value.trim())) throw new Error(t("请从主会话创建侧聊"));
    return this.actions.send(options);
  }

  resumeTurn() { if (!this.deleting) return this.actions.resume(); }
  addFiles(files, kind = 'file') { if (!this.uploadingFiles) return this.actions.addFiles(files, kind); }

  setRequests(requests) { this.requests = requests; this.renderRequests(); }

  renderRequests() {
    const requests = this.requests.filter(request => request.machineId === this.parent?.machineId && request.threadId === this.id);
    const signature = JSON.stringify([locale(), requests]);
    if (signature === this.requestSignature || $('requests').querySelector('[data-composing]')) return;
    this.requestSignature = signature;
    renderRequestCards($('requests'), requests);
    enhanceSelects($('requests'));
  }

  answer(form) { return this.actions.answer(form); }

  resize(bottom = false) {
    $('input').style.height = 'auto';
    $('input').style.height = `${$('input').scrollHeight}px`;
    this.render(bottom);
  }

  editMessage(itemId, turnId, options) {
    const editing = this.actions.editMessage(itemId, turnId, options);
    if (!editing || options?.regenerate) return editing;
    $('conversation').querySelector('.message-edit-input')?.focus();
  }
  regenerateTurn(turnId) { return this.actions.regenerateTurn(turnId); }
  regenerateMessage(itemId, turnId) { return this.actions.regenerateMessage(itemId, turnId); }
  saveEditedMessage() { return this.actions.saveEditedMessage(); }

  bindEvents() {
    $('close').addEventListener('click', () => { this.saveDraft(); this.show(false); document.getElementById('message-input').focus(); });
    $('new').addEventListener('click', this.run(() => this.open({ fresh: true })));
    $('delete').addEventListener('click', this.run(() => this.deleteCurrent()));
    $('shared').addEventListener('toggle', () => {
      if ($('shared').open) $('context').scrollTop = $('context').scrollHeight;
    });
    $('context').addEventListener('click', event => {
      const image = event.target.closest('[data-preview-image]')?.querySelector('img');
      if (image) this.openImage(image.src);
    });
    $('tabs').addEventListener('click', this.run(event => { const id = event.target.closest('[data-side-id]')?.dataset.sideId; if (id) return this.select(id); }));
    $('tabs').addEventListener('keydown', this.run(event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = this.ids.indexOf(this.id);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? this.ids.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + this.ids.length) % this.ids.length;
      return this.select(this.ids[next]).then(() => $('tabs').querySelector('[aria-selected="true"]')?.focus());
    }));
    $('older').addEventListener('click', this.run(() => this.older()));
    $('form').addEventListener('submit', this.run(event => { event.preventDefault(); return this.send(); }));
    $('input').addEventListener('input', () => { this.saveDraft(); this.resize(); });
    $('model').addEventListener('change', this.run(() => this.patch({ model: $('model').value })));
    $('effort').addEventListener('change', this.run(() => this.patch({ effort: $('effort').value })));
    $('fast').addEventListener('click', this.run(() => this.patch({ serviceTier: composer.nextSpeedTier(this.models(), this.settings.model, this.settings.serviceTier) })));
    $('tool-compact').addEventListener('click', this.run(() => this.patch({ toolCallCompact: !this.settings.toolCallCompact })));
    for (const id of ['interrupt', 'working-interrupt']) $(id).addEventListener('click', this.run(() => this.actions.interrupt()));
    for (const id of ['resume', 'working-resume']) $(id).addEventListener('click', this.run(() => this.resumeTurn()));
    $('conversation').addEventListener('click', this.run(async event => {
      const menuAction = event.target.closest('.turn-menu button');
      if (menuAction) menuAction.closest('.turn-menu').open = false;
      const image = event.target.closest('[data-preview-image]')?.querySelector('img');
      if (image) return this.openImage(image.src);
      const agent = event.target.closest('[data-open-agent]');
      if (agent) return this.openThread(this.parent.machineId, agent.dataset.openAgent);
      const forkMessage = event.target.closest('[data-fork-message]');
      if (forkMessage) {
        return this.fork({ turnId: forkMessage.closest('[data-turn-id]').dataset.turnId, lastItemId: forkMessage.dataset.forkMessage });
      }
      const regenerateMessage = event.target.closest('[data-regenerate-message]');
      if (regenerateMessage) return this.regenerateMessage(regenerateMessage.dataset.regenerateMessage, regenerateMessage.closest('[data-turn-id]').dataset.turnId);
      const fork = event.target.closest('[data-fork-turn]');
      if (fork) return this.fork({ lastTurnId: fork.dataset.forkTurn });
      const regenerate = event.target.closest('[data-regenerate-turn]');
      if (regenerate) return this.regenerateTurn(regenerate.dataset.regenerateTurn);
      const copyMessage = event.target.closest('[data-copy-message]');
      if (copyMessage) {
        const turn = this.conversation.visibleTurns.find(turn => turn.id === copyMessage.closest('[data-turn-id]').dataset.turnId);
        const item = turn.items.find(item => item.id === copyMessage.dataset.copyMessage);
        await navigator.clipboard.writeText(view.itemText(item));
        return this.toast(t("消息已复制"));
      }
      const copy = event.target.closest('[data-copy-turn]');
      if (copy) {
        const turn = this.conversation.turns.find(turn => turn.id === copy.dataset.copyTurn);
        await navigator.clipboard.writeText(turn.items.filter(item => item.type === 'agentMessage').map(view.itemText).join('\n\n'));
        return this.toast(t("本轮回复已复制"));
      }
      if (event.target.closest('[data-cancel-edit]')) return this.actions.cancelEdit();
      const edit = event.target.closest('[data-edit-message]');
      if (edit) return this.editMessage(edit.dataset.editMessage, edit.closest('[data-turn-id]').dataset.turnId);
    }));
    $('conversation').addEventListener('submit', this.run(event => {
      event.preventDefault();
      if (event.target.matches('[data-question-form]')) return this.answer(event.target);
      return this.saveEditedMessage();
    }));
    $('requests').addEventListener('submit', this.run(event => { event.preventDefault(); return this.answer(event.target); }));
    for (const container of [$('conversation'), $('context')]) container.addEventListener('nexus/visualization-follow-up', this.run(async event => {
      await api(`${this.path}/messages`, { method: 'POST', body: { text: event.detail.prompt, actor: 'human' } });
    }));
    $('requests').addEventListener('click', this.run(event => this.actions.respond(event.target.closest('button'))));
    for (const container of [$('requests'), $('conversation')]) bindQuestionInputs(container, () => this.render());
    $('upload-menu').addEventListener('toggle', event => {
      if (event.newState !== 'open') return;
      const rect = $('attach').getBoundingClientRect();
      $('upload-menu').style.left = `${Math.min(rect.left, innerWidth - $('upload-menu').offsetWidth - 12)}px`;
      $('upload-menu').style.top = `${rect.top - $('upload-menu').offsetHeight - 8}px`;
    });
    $('upload-menu').addEventListener('click', event => { const choice = event.target.closest('[data-side-upload]'); if (choice) { $('upload-menu').hidePopover(); $(choice.dataset.sideUpload === 'image' ? 'images' : 'files').click(); } });
    for (const kind of ['files', 'images']) $(kind).addEventListener('change', this.run(event => { const files = [...event.target.files]; event.target.value = ''; return this.addFiles(files, kind === 'images' ? 'image' : 'file'); }));
    $('input').addEventListener('paste', this.run(event => { const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === 'file').map(item => item.getAsFile()); if (files.length) { event.preventDefault(); return this.addFiles(files, files.every(file => file.type.startsWith('image/')) ? 'image' : 'file'); } }));
    const area = $('form').closest('.composer-area');
    area.addEventListener('dragover', event => { if ([...event.dataTransfer.items].some(item => item.kind === 'file')) { event.preventDefault(); area.classList.add('drag-over'); } });
    area.addEventListener('dragleave', event => { if (!area.contains(event.relatedTarget)) area.classList.remove('drag-over'); });
    area.addEventListener('drop', this.run(event => {
      event.preventDefault(); area.classList.remove('drag-over');
      const files = [...event.dataTransfer.files];
      return this.addFiles(files, event.shiftKey && files.every(file => file.type.startsWith('image/')) ? 'image' : 'file');
    }));
    $('attachments').addEventListener('click', event => {
      const preview = event.target.closest('[data-side-preview]');
      if (preview) return this.openImage(this.attachments.get(this.actions.key)[Number(preview.dataset.sidePreview)].preview);
      const remove = event.target.closest('[data-side-remove]');
      if (remove) { this.attachments.get(this.actions.key).splice(Number(remove.dataset.sideRemove), 1); this.render(); }
    });
  }
}
