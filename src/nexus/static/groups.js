import { t, locale } from './i18n.js?v=145';
import { api, query, threadPath } from './api.js?v=145';
import { icon } from './icons.js?v=145';
import { h, markdown, basename, projectFilter, sessionSections, identicon } from './views.js?v=145';

const $ = id => document.getElementById(id);
const path = name => `/groups/${encodeURIComponent(name)}`;
const draftKey = name => `nexus-group-draft:${name}`;

function groupAvatar(members) {
  return members.length ? `<span class="group-avatar-grid ${members.length === 1 ? 'single' : ''}">${members.slice(0, 4).map(member => identicon(member.botId)).join('')}</span>` : icon('messages');
}

export class GroupChat {
  constructor({ toast, openModal, closeModal, onOpenNavigation, onNavigate, onOpenThread, requests }) {
    Object.assign(this, { toast, openModal, closeModal, onNavigate, onOpenThread, requests });
    this.groups = {};
    this.creationOrder = [];
    this.summaries = {};
    this.cache = new Map();
    this.rows = new Map();
    this.nodes = new Map();
    this.sending = new Set();
    this.errors = new Map();
    this.visible = false;
    this.loaded = false;
    this.metadataVersion = 0;
    $('groups-open-nav').addEventListener('click', onOpenNavigation);
    $('groups-new').addEventListener('click', () => this.edit().catch(error => toast(error.message, true)));
    $('group-welcome-new').addEventListener('click', () => this.edit().catch(error => toast(error.message, true)));
    $('group-edit').addEventListener('click', () => this.edit(this.selected).catch(error => toast(error.message, true)));
    $('group-members').addEventListener('click', () => this.edit(this.selected).catch(error => toast(error.message, true)));
    $('group-delete').addEventListener('click', () => this.remove());
    $('groups-refresh').addEventListener('click', () => this.refresh().catch(error => this.listError(error.message)));
    $('groups-search').addEventListener('input', () => this.renderList());
    $('groups-sort').addEventListener('change', () => this.renderList());
    $('groups-list').addEventListener('click', event => {
      const row = event.target.closest('[data-group-name]');
      if (row) this.select(row.dataset.groupName).catch(error => this.listError(error.message));
    });
    $('group-back').addEventListener('click', () => {
      $('groups-workspace').classList.remove('group-selected');
      this.onNavigate(null);
    });
    $('group-history-more').addEventListener('click', () => this.history(this.selected, true));
    $('group-message-form').addEventListener('submit', event => { event.preventDefault(); this.send(); });
    $('group-message-input').addEventListener('input', () => {
      if (this.selected) {
        localStorage.setItem(draftKey(this.selected), $('group-message-input').value);
        this.errors.delete(this.selected);
      }
      this.controls();
      this.resizeInput();
    });
    $('group-messages').addEventListener('click', async event => {
      const sender = event.target.closest('[data-group-sender]');
      if (!sender) return;
      try {
        const address = await api(`/threads/resolve${query({ threadId: sender.dataset.groupSender })}`);
        await this.onOpenThread(address.machineId, address.threadId);
      } catch (error) { toast(error.message, true); }
    });
  }

  conversation(name) {
    if (!this.cache.has(name)) this.cache.set(name, { messages: new Map(), loaded: false, loading: false, loadingMore: false, cursor: null });
    return this.cache.get(name);
  }

  listError(message = '') {
    $('groups-list-error').textContent = message;
    $('groups-list-error').hidden = !message;
  }

  async open(name) {
    this.visible = true;
    await this.refresh();
    const saved = localStorage.getItem('nexus-group');
    const selected = name || this.selected || (saved in this.groups ? saved : Object.keys(this.groups)[0]);
    if (selected && selected in this.groups && (name || innerWidth > 720)) await this.select(selected);
    else {
      $('groups-workspace').classList.remove('group-selected');
      this.onNavigate(null);
    }
  }

  hide() { this.visible = false; }

  async refresh() {
    if (this.refreshing) return this.refreshing;
    this.refreshing = this.refreshGroups().finally(() => { this.refreshing = null; });
    return this.refreshing;
  }

  async refreshGroups() {
    const version = this.metadataVersion;
    const result = await api('/groups');
    if (version !== this.metadataVersion) return this.refreshGroups();
    this.loaded = true;
    this.groups = result.groups;
    this.creationOrder = result.creationOrder;
    for (const [name, summary] of Object.entries(result.summaries || {})) {
      if ((summary.lastMessage?.sequence || 0) >= (this.summaries[name]?.lastMessage?.sequence || 0)) this.summaries[name] = summary;
    }
    this.listError();
    for (const name of this.cache.keys()) if (!(name in this.groups)) this.cache.delete(name);
    if (this.selected && !(this.selected in this.groups)) this.clear();
    this.renderList();
    this.renderHeader();
    if (this.visible && this.selected) await this.history(this.selected);
  }

  clear() {
    this.selected = null;
    this.nodes.clear();
    $('group-messages').replaceChildren();
    $('group-view').hidden = true;
    $('group-welcome').hidden = false;
    $('groups-workspace').classList.remove('group-selected');
    if (this.visible) this.onNavigate(null);
  }

  async select(name) {
    if (!(name in this.groups)) return;
    if (this.selected !== name) {
      this.selected = name;
      this.nodes.clear();
      $('group-messages').replaceChildren();
      $('group-message-input').value = localStorage.getItem(draftKey(name)) || '';
      $('group-scroll').scrollTop = 0;
    }
    localStorage.setItem('nexus-group', name);
    $('group-welcome').hidden = true;
    $('group-view').hidden = false;
    $('groups-workspace').classList.add('group-selected');
    this.onNavigate(name);
    this.renderList();
    this.renderHeader();
    this.renderMessages(true);
    this.controls();
    this.resizeInput();
    await this.history(name);
  }

  renderList() {
    const search = $('groups-search').value.trim().toLowerCase();
    const names = [...this.creationOrder].reverse().filter(name => name in this.groups && name.toLowerCase().includes(search));
    if ($('groups-sort').value === 'activity') names.sort((a, b) => (this.summaries[b]?.lastMessage?.sequence || 0) - (this.summaries[a]?.lastMessage?.sequence || 0));
    const root = $('groups-list');
    if (!names.length) {
      root.innerHTML = `<div class="list-empty">${!this.loaded ? t("正在读取群聊…") : search ? t("没有匹配的群聊") : t("暂无群聊")}</div>`;
      this.rows.clear();
      return;
    }
    root.querySelector('.list-empty')?.remove();
    let previous = null;
    for (const name of names) {
      const members = this.groups[name];
      const last = this.summaries[name]?.lastMessage;
      let row = this.rows.get(name);
      if (!row) {
        row = document.createElement('button');
        row.type = 'button';
        row.className = 'group-row';
        row.dataset.groupName = name;
        this.rows.set(name, row);
      }
      row.classList.toggle('selected', name === this.selected);
      row.setAttribute('aria-current', name === this.selected ? 'true' : 'false');
      const signature = JSON.stringify([locale(), members, last]);
      if (row._signature !== signature) {
        row._signature = signature;
        const time = last ? new Date(last.createdAt).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
        const preview = last ? `${last.senderKind === 'human' ? t("用户") : last.senderBotId}: ${(last.text || t("[附件]")).replace(/\s+/g, ' ')}` : t("暂无消息");
        row.innerHTML = `<span class="group-row-avatar">${groupAvatar(members)}</span><span class="group-row-content"><span class="group-row-heading"><strong>${h(name)}</strong><time>${h(time)}</time></span><span class="group-row-preview">${h(preview)}</span></span>`;
      }
      if (row !== (previous ? previous.nextSibling : root.firstChild)) root.insertBefore(row, previous ? previous.nextSibling : root.firstChild);
      previous = row;
    }
    for (const [name, row] of this.rows) if (!names.includes(name)) { row.remove(); this.rows.delete(name); }
  }

  renderHeader() {
    if (!this.selected || !(this.selected in this.groups)) return;
    const members = this.groups[this.selected];
    $('group-title').textContent = this.selected;
    $('group-header-avatar').innerHTML = groupAvatar(members);
    $('group-members').textContent = t("{0} 位成员", members.length);
  }

  merge(name, messages) {
    const cache = this.conversation(name);
    for (const message of messages) {
      const previous = cache.messages.get(message.id);
      if (!previous || (message.revision || 0) >= (previous.revision || 0)) cache.messages.set(message.id, message);
    }
  }

  async history(name, older = false) {
    if (!name) return;
    const cache = this.conversation(name);
    if (older ? cache.loadingMore || !cache.cursor : cache.loading) return;
    const wasLoaded = cache.loaded;
    if (older) cache.loadingMore = true;
    else cache.loading = true;
    if (this.selected === name) this.renderMessages();
    try {
      const result = await api(`${path(name)}/messages${query({ before: older ? cache.cursor : undefined })}`);
      this.merge(name, result.data);
      if (older || !cache.loaded) cache.cursor = result.nextCursor;
      cache.loaded = true;
      cache.error = '';
    } catch (error) { cache.error = error.message; }
    finally {
      cache.loading = false;
      cache.loadingMore = false;
      if (this.selected === name) this.renderMessages(!older && !wasLoaded);
    }
  }

  renderMessages(forceBottom = false) {
    if (!this.selected) return;
    const cache = this.conversation(this.selected);
    const messages = [...cache.messages.values()].sort((a, b) => (a.sequence || Infinity) - (b.sequence || Infinity) || a.createdAt.localeCompare(b.createdAt));
    const root = $('group-messages');
    const scroll = $('group-scroll');
    const beforeHeight = scroll.scrollHeight;
    const beforeTop = scroll.scrollTop;
    const anchor = root.querySelector('.group-message');
    const anchorTop = anchor?.offsetTop || 0;
    const nearBottom = beforeHeight - beforeTop - scroll.clientHeight < 90;
    const status = $('group-history-status');
    status.hidden = Boolean(messages.length) && !cache.error;
    status.textContent = cache.error || (cache.loaded ? messages.length ? '' : t("暂无消息") : t("正在读取消息…"));
    $('group-history-more').hidden = !cache.cursor;
    $('group-history-more').disabled = cache.loadingMore;
    let previous = null;
    let day = '';
    const used = new Set();
    for (const message of messages) {
      const date = new Date(message.createdAt);
      const dateLabel = date.toLocaleDateString(locale(), { year: 'numeric', month: 'long', day: 'numeric' });
      if (dateLabel !== day) {
        day = dateLabel;
        const key = `day:${day}`;
        let divider = this.nodes.get(key);
        if (!divider) { divider = document.createElement('div'); divider.className = 'group-day'; divider.textContent = day; this.nodes.set(key, divider); }
        if (divider !== (previous ? previous.nextSibling : root.firstChild)) root.insertBefore(divider, previous ? previous.nextSibling : root.firstChild);
        previous = divider;
        used.add(key);
      }
      let node = this.nodes.get(message.id);
      if (!node) {
        node = document.createElement('article');
        node.className = `group-message${message.senderKind === 'human' ? ' own' : ''}`;
        node.dataset.messageId = message.id;
        const name = message.senderKind === 'human' ? t("用户") : message.senderBotId;
        const avatar = message.senderKind === 'human' ? `<span class="group-user-avatar">${icon('user')}</span>` : identicon(name);
        const sender = message.senderKind === 'human' ? `<span>${name}</span>` : `<button type="button" data-group-sender="${h(message.senderThreadId)}" data-tooltip="${t("查看会话")}" data-i18n-data-tooltip="查看会话">${h(name)}</button>`;
        node.innerHTML = `<span class="group-message-avatar">${avatar}</span><div class="group-message-content"><div class="group-message-meta">${sender}<time datetime="${h(message.createdAt)}" data-tooltip="${h(date.toLocaleString(locale()))}">${h(date.toLocaleTimeString(locale(), { hour12: false }))}</time><span class="group-delivery"></span></div><div class="group-bubble markdown">${markdown(message.text || t("[附件]"))}</div></div>`;
        this.nodes.set(message.id, node);
      }
      const sender = node.querySelector('.group-message-meta > span');
      if (message.senderKind === 'human') sender.textContent = t("用户");
      const delivery = node.querySelector('.group-delivery');
      const time = node.querySelector('time');
      time.dateTime = message.createdAt;
      time.dataset.tooltip = date.toLocaleString(locale());
      time.textContent = date.toLocaleTimeString(locale(), { hour12: false });
      delivery.className = `group-delivery ${message.status}`;
      delivery.textContent = ({ pending: t("发送中"), partial: t("部分未送达"), failed: t("未送达") })[message.status] || '';
      delivery.dataset.tooltip = (message.delivery?.errors || []).map(item => `${item.botId || ''}: ${item.error}`).join('\n');
      if (node !== (previous ? previous.nextSibling : root.firstChild)) root.insertBefore(node, previous ? previous.nextSibling : root.firstChild);
      previous = node;
      used.add(message.id);
    }
    for (const [key, node] of this.nodes) if (!used.has(key)) { node.remove(); this.nodes.delete(key); }
    if (forceBottom || nearBottom) scroll.scrollTop = scroll.scrollHeight;
    else scroll.scrollTop = beforeTop + (anchor?.isConnected ? anchor.offsetTop - anchorTop : 0);
  }

  controls() {
    $('group-send').disabled = !this.selected || !this.groups[this.selected]?.length || !$('group-message-input').value.trim() || this.sending.has(this.selected);
    $('group-send').setAttribute('aria-busy', String(this.sending.has(this.selected)));
    const error = this.errors.get(this.selected) || '';
    $('group-send-error').hidden = !error;
    $('group-send-error').textContent = error;
  }

  resizeInput() {
    const input = $('group-message-input');
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  async send() {
    const name = this.selected;
    const text = $('group-message-input').value.trim();
    if (!name || !text || this.sending.has(name)) return;
    const id = [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, '0')).join('');
    this.sending.add(name);
    this.merge(name, [{ id, groupName: name, senderKind: 'human', text, createdAt: new Date().toISOString(), status: 'pending', revision: 0 }]);
    this.renderMessages(true);
    this.controls();
    try {
      const result = await api(`${path(name)}/messages`, { method: 'POST', body: { text, clientMessageId: id } });
      this.merge(name, [result.message]);
      if (result.errors.length && !result.data.length) throw new Error(result.errors.map(item => `${item.botId}: ${item.error}`).join('；'));
      this.errors.delete(name);
      if (this.selected === name && $('group-message-input').value.trim() === text) {
        $('group-message-input').value = '';
        localStorage.removeItem(draftKey(name));
        this.resizeInput();
      } else if (localStorage.getItem(draftKey(name))?.trim() === text) localStorage.removeItem(draftKey(name));
    } catch (error) {
      this.errors.set(name, error.message);
      const message = this.conversation(name).messages.get(id);
      if (!message.sequence) message.status = 'failed';
    } finally {
      this.sending.delete(name);
      if (this.selected === name) { this.renderMessages(); this.controls(); }
    }
  }

  handleEvent(event) {
    const data = event.params || {};
    if (event.method === 'nexus/group/message') {
      this.merge(data.groupName, [data.message]);
      const previous = this.summaries[data.groupName];
      if ((data.message.sequence || 0) > (previous?.lastMessage?.sequence || 0)) this.summaries[data.groupName] = { messageCount: (previous?.messageCount || 0) + 1, lastMessage: data.message };
      this.renderList();
      if (data.groupName === this.selected) this.renderMessages();
    } else if (event.method === 'nexus/group/updated') {
      this.metadataVersion++;
      this.groups[data.groupName] = data.members;
      if (!this.creationOrder.includes(data.groupName)) this.creationOrder.push(data.groupName);
      this.renderList();
      this.renderHeader();
      this.controls();
    } else if (event.method === 'nexus/group/deleted') {
      this.metadataVersion++;
      delete this.groups[data.groupName];
      this.creationOrder = this.creationOrder.filter(name => name !== data.groupName);
      delete this.summaries[data.groupName];
      this.cache.delete(data.groupName);
      if (this.selected === data.groupName) this.clear();
      this.renderList();
    } else if (event.method === 'nexus/resync' && this.visible) this.refresh().catch(error => this.listError(error.message));
  }

  async edit(name) {
    const { data } = await api('/bots');
    const existing = this.groups[name] || [];
    const bots = new Map(data.map(item => [item.threadId, { ...item, id: item.threadId }]));
    const eligible = new Set(bots.keys());
    for (const member of existing) if (!bots.has(member.threadId)) bots.set(member.threadId, { ...member, id: member.threadId });
    const selected = new Set(existing.map(item => item.threadId));
    const visited = new Set(selected);
    const projects = [...new Map(data.map(item => [projectFilter(item.machineId, item.cwd), item])).entries()]
      .sort(([, a], [, b]) => a.machineName.localeCompare(b.machineName) || a.cwd.localeCompare(b.cwd));
    this.openModal({
      title: name ? t("管理群成员") : t("新建群聊"),
      submitLabel: name ? t("保存") : t("创建"),
      body: `<label class="field"><span data-i18n="群名">${t("群名")}</span><input name="groupName" value="${h(name || '')}" required autocomplete="off" ${name ? 'readonly' : ''}></label><div class="group-picker-layout"><section class="group-picker-pane"><div class="group-picker-heading"><span data-i18n="已选会话">${t("已选会话")}</span><span id="group-picker-count"></span></div><div id="group-selected-picker" class="group-member-picker" aria-label="${t("已选及本次选过的会话")}" data-i18n-aria-label="已选及本次选过的会话"></div></section><section class="group-picker-pane"><div class="group-picker-heading"><span data-i18n="可选会话">${t("可选会话")}</span></div><div class="group-picker-project"><select id="group-picker-project" aria-label="${t("按项目筛选")}" data-i18n-aria-label="按项目筛选"><option value="" data-i18n="全部项目">${t("全部项目")}</option>${projects.map(([key, item]) => `<option value="${h(key)}">${h(item.machineName)} · ${h(item.cwd)}</option>`).join('')}</select></div><div class="search-field group-picker-search">${icon('search')}<input id="group-picker-search" type="search" placeholder="${t("搜索会话")}" aria-label="${t("搜索成员")}" autocomplete="off" data-i18n-placeholder="搜索会话" data-i18n-aria-label="搜索成员"><select id="group-picker-mode" aria-label="${t("成员搜索模式")}" data-i18n-aria-label="成员搜索模式"><option value="title" data-i18n="标题">${t("标题")}</option><option value="id">ID</option></select></div><div id="group-member-picker" class="group-member-picker" aria-label="${t("剩余候选会话")}" data-i18n-aria-label="剩余候选会话"></div></section></div>`,
      afterRender: () => {
        const option = item => `<label class="group-member-option" data-tooltip="${h(item.machineName || item.machineId)}${item.cwd ? ` · ${h(item.cwd)}` : ''}">${identicon(item.botId)}<span><strong>${h(item.botId)}</strong><span>${h(item.name || (item.cwd ? `${item.machineName} · ${basename(item.cwd)}` : item.threadId))}</span></span><input type="checkbox" value="${h(item.threadId)}" ${selected.has(item.threadId) ? 'checked' : eligible.has(item.threadId) ? '' : 'disabled'}></label>`;
        const render = () => {
          const search = $('group-picker-search').value.trim().toLowerCase();
          const byId = $('group-picker-mode').value === 'id';
          const project = $('group-picker-project').value;
          const matches = data.map(item => bots.get(item.threadId)).filter(item => {
            if (visited.has(item.threadId)) return false;
            if (byId && search) return item.threadId.toLowerCase().endsWith(search);
            return (!project || projectFilter(item.machineId, item.cwd) === project)
              && (!search || `${item.name || ''} ${item.machineName || ''}`.toLowerCase().includes(search));
          });
          $('group-picker-search').placeholder = byId ? t("搜索会话 ID") : t("搜索会话");
          $('group-picker-count').textContent = selected.size;
          const left = $('group-selected-picker'), right = $('group-member-picker');
          const leftTop = left.scrollTop, rightTop = right.scrollTop;
          left.innerHTML = visited.size ? [...visited].map(id => option(bots.get(id))).join('') : `<div class="list-empty" data-i18n="暂无已选会话">${t("暂无已选会话")}</div>`;
          right.innerHTML = matches.length ? sessionSections(matches, this.requests()).filter(group => group.threads.length).map(group => `<section class="session-group"><h2 class="session-status-heading">${group.label}<span>${group.threads.length}</span></h2>${group.threads.map(option).join('')}</section>`).join('') : `<div class="list-empty" data-i18n="没有匹配的会话">${t("没有匹配的会话")}</div>`;
          left.scrollTop = leftTop;
          right.scrollTop = rightTop;
        };
        $('group-picker-search').addEventListener('input', render);
        $('group-picker-mode').addEventListener('change', render);
        $('group-picker-project').addEventListener('change', render);
        $('modal-body').querySelector('.group-picker-layout').addEventListener('change', event => {
          if (event.target.type !== 'checkbox') return;
          if (event.target.checked) {
            selected.add(event.target.value);
            visited.add(event.target.value);
          }
          else selected.delete(event.target.value);
          if (event.target.closest('#group-member-picker')) {
            render();
            [...$('group-selected-picker').querySelectorAll('input')].find(input => input.value === event.target.value)?.focus({ preventScroll: true });
          }
          else {
            $('group-picker-count').textContent = selected.size;
            event.target.disabled = !event.target.checked && !eligible.has(event.target.value);
          }
          $('modal-error').hidden = true;
        });
        $('modal-body').querySelector('[name="groupName"]').addEventListener('input', () => { $('modal-error').hidden = true; });
        render();
      },
      onSubmit: async form => {
        const group = name || form.get('groupName').trim();
        if (!selected.size && !name) throw new Error(t("请选择群成员"));
        if (!name && group in this.groups) throw new Error(t("群名已存在"));
        const result = await api(path(group), { method: 'PUT', body: { threadIds: [...visited].filter(id => selected.has(id)) } });
        this.groups[group] = result.members;
        if (!this.creationOrder.includes(group)) this.creationOrder.push(group);
        this.closeModal();
        await this.select(group);
      },
    });
  }

  async manageThread(thread) {
    const [, { data: eligible }] = await Promise.all([this.refresh(), api('/bots')]);
    const canJoin = eligible.some(item => item.machineId === thread.machineId && item.threadId === thread.threadId);
    const includes = members => members.some(item => item.machineId === thread.machineId && item.threadId === thread.threadId);
    const selected = new Set(Object.keys(this.groups).filter(name => includes(this.groups[name])));
    this.openModal({
      title: t("所在群聊"),
      body: `<div id="thread-group-picker" class="thread-group-picker">${Object.entries(this.groups).map(([name, members]) => `<label class="group-member-option"><span class="group-row-avatar">${groupAvatar(members)}</span><span class="thread-group-name">${h(name)}</span><input type="checkbox" value="${h(name)}" ${selected.has(name) ? 'checked' : canJoin ? '' : 'disabled'}></label>`).join('') || `<div class="list-empty" data-i18n="暂无群聊">${t("暂无群聊")}</div>`}</div>`,
      afterRender: () => {
        $('thread-group-picker').addEventListener('change', event => {
          if (event.target.checked) selected.add(event.target.value);
          else selected.delete(event.target.value);
          if (!canJoin) event.target.disabled = !event.target.checked;
          $('modal-error').hidden = true;
        });
      },
      onSubmit: async () => {
        const result = await api(`${threadPath(thread.machineId, thread.threadId)}/groups`, { method: 'PUT', body: { names: [...selected] } });
        this.groups = result.groups;
        this.renderList();
        this.renderHeader();
        this.controls();
        this.closeModal();
      },
    });
  }

  remove() {
    const name = this.selected;
    if (!name) return;
    this.openModal({
      title: t("删除群聊"), submitLabel: t("删除"),
      body: `<p class="group-delete-confirm">${t("删除「{0}」及其群消息记录？", h(name))}</p>`,
      onSubmit: async () => {
        await api(path(name), { method: 'DELETE' });
        delete this.groups[name];
        this.creationOrder = this.creationOrder.filter(group => group !== name);
        delete this.summaries[name];
        this.cache.delete(name);
        localStorage.removeItem(draftKey(name));
        this.closeModal();
        if (this.selected === name) this.clear();
        this.renderList();
      },
    });
  }
}
