import { t, locale, setLanguage } from './i18n.js?v=145';
import { api, query, threadPath, openEvents } from './api.js?v=145';
import { icon, hydrateIcons } from './icons.js?v=145';
import * as view from './views.js?v=145';
import { ConversationRenderer } from './conversation.js?v=145';
import { ConversationController } from './conversation-controller.js?v=145';
import { ConversationActions, draftStorageKey, setQuestionBusy, bindQuestionInputs, renderRequestCards } from './conversation-actions.js?v=145';
import * as composer from './composer.js?v=145';
import { GoalPanel } from './goals.js?v=145';
import { enhanceSelects, closeSelectMenu, updateSelectOptions, setSettingsDisabled } from './select.js?v=145';
import { featuresPanel, bindFeatures, toolCompactControl } from './features.js?v=145';
import { installApp, installLabel, setupPWA, notificationStatus, notificationMessages, toggleNotifications, restoreNotifications, testNotification } from './pwa.js?v=145';
import { FileBrowser } from './file-browser.js?v=145';
import { TerminalBrowser } from './terminals.js?v=145';
import { GroupChat } from './groups.js?v=145';
import * as throughput from './throughput.js?v=145';
import { RightPanel, setupPaneResizing } from './layout.js?v=145';
import { setupMachineFilter } from './machine-filter.js?v=145';
import { SidePanel } from './side.js?v=145';
import { DeveloperPanel } from './developer.js?v=145';
import { ContextPanel } from './context.js?v=145';
import { AccountLimits } from './accounts.js?v=145';
import { setupTextContextMenu, showContextMenu, copyText } from './context-menu.js?v=145';
import './tooltip.js?v=145';

const $ = id => document.getElementById(id);
const state = {
  config: {}, machines: [], threads: [], liveThreads: [], requests: [], errors: [],
  section: 'sessions', filter: 'sessions', search: '', searchMode: 'both', selected: null, detail: null,
  filesFromSession: false,
  list: { sources: [], loading: false, loadingMore: false }, recentLimit: 10,
  sessionGroups: { unarchived: true, archived: false }, collapsedMachines: new Set(),
  machineActions: new Set(),
  tasks: [], tasksOpen: false, tasksLoading: false, tasksError: '', sending: false,
  attachments: new Map(), skillInputs: new Map(), models: new Map(),
  currentModels: [], requestSignature: '', modalHandler: null,
  eventEpoch: null,
  settings: {}, tokenUsage: null, settingsSaving: false,
  contextSignature: '',
  editing: null,
  uploadingFiles: 0,
  online: true,
  initializing: true, machinesLoaded: false,
  slashIndex: 0, slashCommands: [], slashDismissed: false,
};
const endpoint = () => threadPath(state.selected.machineId, state.selected.threadId);
const selectedKey = () => state.selected && `${state.selected.machineId}/${state.selected.threadId}`;
const machine = id => state.machines.find(machine => machine.id === id);
const trackedThread = thread => Boolean(machine(thread.machineId)?.projects?.includes(thread.cwd));
const listedThread = thread => thread.threadSource !== 'nexus-side' && !thread.source?.subAgent;
const currentProject = () => view.projectFromFilter(state.filter);
const listProject = () => state.search.trim() && state.searchMode === 'id' ? null : currentProject();
const asThread = thread => ({ ...thread, machineId: thread.machineId || state.selected?.machineId, machineName: thread.machineName || machine(thread.machineId || state.selected?.machineId)?.name });
const run = fn => (...args) => Promise.resolve(fn(...args)).catch(error => toast(error.message, true));
let listRefreshTimer;
let searchTimer;
let renderFrame;
let events;
let turnTimer;
const conversationRenderer = new ConversationRenderer($('conversation'));
const conversation = new ConversationController({
  onSnapshot: result => {
    if (result.thread.sideConversation) {
      const { machineId, threadId } = conversation.selection;
      const parentId = result.thread.sideConversation.parentThreadId;
      selectThread(machineId, parentId).then(() => {
        if (selectedKey() === `${machineId}/${parentId}`) return sidePanel.select(threadId);
      }).catch(error => showThreadError(error.message));
      return;
    }
    applySessionState(result);
    updateThread(state.detail);
  },
  onEvent: handleThreadEvent,
  onChange: ({ initial, changed }) => {
    if (initial || changed) renderConversation(initial);
    else refreshComposer();
    renderList();
    if (initial && state.tasksOpen) refreshTasks().catch(() => {});
  },
  onError: error => showThreadError(error.message),
});
const actions = new ConversationActions({
  conversation, state, input: $('message-input'), transcript: $('conversation'),
  thread: () => state.detail,
  onChange: bottom => { if (bottom) resizeComposer(); renderConversation(bottom); },
  onCommit: context => {
    if (context.archived) applyArchived(context.selection.machineId, context.selection.threadId, false);
    if (context.selection === conversation.selection) showThreadError();
    scheduleListRefresh();
  },
  onSettings: applySessionState,
  refreshRequests,
});
const rightPanel = new RightPanel();
const goalPanel = new GoalPanel({
  element: $('goal-panel'), button: $('composer-goal'), conversation, openModal, closeModal,
});
const sidePanel = new SidePanel({
  rightPanel,
  toast, models: () => state.currentModels, threads: () => state.threads,
  connection: id => !state.online ? t("服务重连中") : machine(id)?.status === 'reconnecting' ? t("机器重连中") : '',
  openThread: selectThread,
  openImage: src => openModal({ title: t("图片"), body: `<img class="image-preview" src="${view.h(src)}" alt="${t("用户图片")}" data-i18n-alt="用户图片">`, footer: false }),
  refreshRequests, openModal, closeModal,
});
const developerPanel = new DeveloperPanel({ rightPanel, toast });
const contextPanel = new ContextPanel({ rightPanel, toast });
const terminalBrowser = new TerminalBrowser({
  toast, onOpen: openTerminals, onOpenNavigation: showNavigation, onOpenThread: selectThread,
  onNavigate: id => {
    if (state.section !== 'terminals') return;
    history.replaceState(null, '', id ? `#terminals/${encodeURIComponent(id)}` : '#terminals');
    document.title = t("终端 · Nexus");
  },
});
const fileBrowser = new FileBrowser({
  toast,
  openModal, closeModal,
  onBack: () => {
    if (state.filesFromSession) showSessions();
    else fileBrowser.browse(fileBrowser.path);
  },
  onOpenTerminal: (machineId, cwd) => terminalBrowser.create(machineId, cwd),
  onOpenNavigation: showNavigation,
  onNavigate: route => {
    if (state.section !== 'files') return;
    history.replaceState(null, '', `#files${query(route)}`);
    document.title = route.file ? `${view.basename(route.file)} · Nexus` : t("文件 · Nexus");
  },
});

const groupChat = new GroupChat({
  toast, openModal, closeModal, onOpenNavigation: showNavigation, onOpenThread: selectThread,
  requests: () => state.requests,
  onNavigate: name => {
    if (state.section !== 'groups') return;
    history.replaceState(null, '', name ? `#groups/${encodeURIComponent(name)}` : '#groups');
    document.title = name ? `${name} · Nexus` : t("群聊 · Nexus");
  },
});

setupTextContextMenu(toast);

function showSessions({ updateHash = true } = {}) {
  state.section = 'sessions';
  state.filesFromSession = false;
  terminalBrowser.hide();
  groupChat.hide();
  $('groups-workspace').hidden = true;
  $('app').classList.remove('files-view', 'terminals-view', 'groups-view');
  $('files-workspace').hidden = true;
  $('terminals-workspace').hidden = true;
  if (updateHash && (location.hash.startsWith('#files') || location.hash.startsWith('#terminals') || location.hash.startsWith('#groups'))) {
    history.replaceState(null, '', state.selected ? `#${encodeURIComponent(state.selected.machineId)}/${encodeURIComponent(state.selected.threadId)}` : location.pathname);
  }
  renderNavigation();
  if (state.detail) renderHeader();
  else document.title = 'Nexus';
}

async function openFiles(route = {}) {
  saveDraft();
  closeSelectMenu();
  if (state.section !== 'files' || !route.file) {
    state.filesFromSession = Boolean(state.section === 'sessions' && state.selected && route.file);
  }
  $('files-preview-back').setAttribute('aria-label', state.filesFromSession ? t("返回会话") : t("返回文件列表"));
  state.section = 'files';
  terminalBrowser.hide();
  groupChat.hide();
  $('groups-workspace').hidden = true;
  $('terminals-workspace').hidden = true;
  $('app').classList.remove('terminals-view', 'groups-view');
  $('app').classList.add('files-view');
  $('files-workspace').hidden = false;
  hideNavigation();
  renderNavigation();
  const machineId = route.machineId || fileBrowser.machineId || state.selected?.machineId;
  const path = route.path || (state.selected && !fileBrowser.paths.has(machineId) && machineId === state.selected.machineId ? state.detail.cwd : undefined);
  await fileBrowser.open({ machines: state.machines, ...route, machineId, path });
}

async function openTerminals(id) {
  saveDraft();
  closeSelectMenu();
  state.section = 'terminals';
  groupChat.hide();
  $('groups-workspace').hidden = true;
  $('app').classList.remove('files-view', 'groups-view');
  $('app').classList.add('terminals-view');
  $('files-workspace').hidden = true;
  $('terminals-workspace').hidden = false;
  $('terminals-workspace').classList.toggle('terminal-selected', Boolean(id));
  hideNavigation();
  renderNavigation();
  await terminalBrowser.open(id);
  if (!id && innerWidth <= 720) $('terminals-workspace').classList.remove('terminal-selected');
}

async function openGroups(name) {
  saveDraft();
  closeSelectMenu();
  state.section = 'groups';
  terminalBrowser.hide();
  $('app').classList.remove('files-view', 'terminals-view');
  $('app').classList.add('groups-view');
  $('files-workspace').hidden = true;
  $('terminals-workspace').hidden = true;
  $('groups-workspace').hidden = false;
  hideNavigation();
  renderNavigation();
  await groupChat.open(name);
}

const fileRoute = () => Object.fromEntries(new URLSearchParams(location.hash.split('?')[1] || ''));

const accountLimits = new AccountLimits(openModal);

hydrateIcons();
enhanceSelects();
setupPaneResizing();
const machineFilter = setupMachineFilter(run(() => changeFilter('sessions')));
setupPWA();
window.addEventListener('nexus:languagechange', () => {
  accountLimits.render();
  closeSelectMenu();
  renderNavigation();
  machineFilter.update(state.machines);
  renderList();
  renderConversation();
  renderRequests();
  renderTasks();
  sidePanel.render();
  terminalBrowser.render();
  groupChat.renderList();
  groupChat.renderHeader();
  groupChat.renderMessages();
  fileBrowser.renderEntries();
  fileBrowser.updateEditorControls();
  if (fileBrowser.file) $('files-preview-mode').textContent = fileBrowser.source ? t('预览') : fileBrowser.editing ? t('编辑') : t('源码');
  if (developerPanel.data) developerPanel.render();
  if (contextPanel.data) contextPanel.render();
  if (state.section === 'files') document.title = fileBrowser.file ? `${fileBrowser.file.name} · Nexus` : t('文件 · Nexus');
  if (state.section === 'groups') groupChat.onNavigate(groupChat.selected);
  if (state.section === 'terminals') document.title = t('终端 · Nexus');
  if ($('language-select')) {
    $('modal-title').textContent = t('设置');
    $('language-select').value = locale();
    $('test-notification').textContent = $('test-notification').disabled ? notificationMessages().pending : t('测试通知');
    const install = $('modal-body').querySelector('[data-install-app]');
    install.lastChild.textContent = installLabel();
    refreshNotificationSettings().catch(error => toast(error.message, true));
  }
});
try { state.collapsedMachines = new Set(JSON.parse(localStorage.getItem('nexus-collapsed-machines') || '[]')); } catch { /* Use expanded machines. */ }

const effortStorageKey = model => `nexus-effort:${selectedKey()}:${model}`;
const savedEffort = model => localStorage.getItem(effortStorageKey(model));

function raiseToasts() {
  const region = $('toast-region');
  if (!region.childElementCount || $('offline-screen').open) return;
  region.hidePopover();
  region.showPopover();
}

function toast(message, error = false, action = null) {
  const node = document.createElement('div');
  node.className = `toast ${error ? 'error' : ''}`;
  node.innerHTML = `${icon(error ? 'circle-alert' : 'check')}<span>${view.h(message)}</span>`;
  const region = $('toast-region');
  const dismiss = () => {
    node.remove();
    if (!region.childElementCount) region.hidePopover();
  };
  if (action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'toast-action';
    button.textContent = action.label;
    button.addEventListener('click', run(async () => {
      button.disabled = true;
      try { await action.onClick(); dismiss(); }
      finally { button.disabled = false; }
    }));
    node.append(button);
  }
  region.append(node);
  raiseToasts();
  setTimeout(dismiss, error || action ? 10000 : 3400);
}

function showThreadError(message = '') {
  $('thread-error').hidden = !message;
  $('thread-error').innerHTML = message ? `${icon('circle-alert')}<span>${view.h(message)}</span>` : '';
}

function hideNavigation() {
  document.querySelector('.sidebar').classList.remove('open');
  $('nav-scrim').hidden = true;
}

function showNavigation() {
  document.querySelector('.sidebar').classList.add('open');
  $('nav-scrim').hidden = false;
}

function updateThread(summary, { live = true } = {}) {
  const key = view.threadKey(summary);
  const index = state.threads.findIndex(thread => view.threadKey(thread) === key);
  if (index < 0) state.threads.unshift(summary);
  else state.threads[index] = { ...state.threads[index], ...summary };
  for (const source of state.list.sources) {
    const item = source.data.find(thread => view.threadKey(thread) === key);
    if (item) Object.assign(item, summary);
  }
  const liveThread = state.liveThreads.find(thread => view.threadKey(thread) === key);
  if (live && liveThread) Object.assign(liveThread, summary);
  if (live && key === selectedKey()) {
    state.detail = { ...state.detail, ...summary };
    if ('archived' in summary && state.selected.archived !== summary.archived) {
      state.selected.archived = summary.archived;
      localStorage.setItem('nexus-selected', JSON.stringify(state.selected));
    }
  }
}

function updateSummaries(machineId, summaries) {
  for (const row of summaries) {
    if (row.threadSource === 'nexus-side') continue;
    const thread = { ...row, machineId, machineName: machine(machineId)?.name };
    const key = view.threadKey(thread);
    updateThread(thread);
    if (!listedThread(thread)) continue;
    state.liveThreads = state.liveThreads.filter(item => view.threadKey(item) !== key);
    if (!thread.archived && view.statusType(thread) !== 'notLoaded') state.liveThreads.push(thread);
    for (const source of state.list.sources) {
      if (source.search) continue;
      const existed = source.data.some(item => view.threadKey(item) === key);
      const oldest = [...source.data].sort(view.byRecent).at(-1);
      source.data = source.data.filter(item => view.threadKey(item) !== key);
      if (source.machineId === machineId && source.archived === Boolean(thread.archived)
          && (existed || !source.cursor || !oldest || view.byRecent(thread, oldest) <= 0)) {
        source.data = mergeThreads(source.data, [thread]);
      }
    }
  }
  renderList();
  if (state.selected) { renderHeader(); refreshComposer(); }
}

function renderNavigation() {
  const filter = state.section === 'sessions' ? state.filter : state.section;
  $('machine-nav').innerHTML = view.machineNav(state.machines, state.threads, filter, state.collapsedMachines, state.online);
  $('main-nav').querySelectorAll('[data-filter]').forEach(button => button.classList.toggle('active', button.dataset.filter === filter));
}

function waitingForThread(thread) {
  return view.isWaiting(thread) || state.requests.some(request => request.machineId === thread.machineId && request.threadId === thread.id);
}

function mergeThreads(...lists) {
  const threads = new Map();
  lists.flat().forEach(thread => threads.set(view.threadKey(thread), { ...threads.get(view.threadKey(thread)), ...thread }));
  return [...threads.values()].sort(view.byRecent);
}

function filteredThreads() {
  const searching = Boolean(state.search.trim());
  let threads = mergeThreads(...state.list.sources.map(source => source.data), searching ? [] : state.liveThreads).filter(thread => trackedThread(thread) && listedThread(thread));
  const project = listProject();
  if (project) threads = threads.filter(thread => thread.machineId === project.machineId && thread.cwd === project.cwd);
  else threads = threads.filter(thread => machineFilter.includes(thread.machineId));
  return threads;
}

function sessionSections(threads = filteredThreads()) {
  return view.sessionSections(threads, state.requests);
}

function renderList() {
  const threads = filteredThreads();
  const project = listProject();
  $('list-title').textContent = project ? view.basename(project.cwd) : t("会话");
  $('list-title').hidden = !project;
  $('machine-filter').hidden = Boolean(project);
  if (project) $('machine-filter-menu').hidePopover();
  $('list-title').dataset.tooltip = project ? `${machine(project.machineId)?.name || project.machineId} · ${project.cwd}` : '';
  $('project-new-thread').hidden = !project;
  const scroller = $('session-list');
  const pending = state.initializing || state.list.loading || state.list.searchPending || (state.search.trim() && state.list.syncing);
  const waiting = pending || state.machines.some(machine => ['connecting', 'reconnecting'].includes(machine.status));
  $('session-loading').hidden = Boolean(state.selected) || !waiting;
  $('welcome').hidden = Boolean(!state.machinesLoaded || state.selected || waiting || threads.length || state.errors.length || state.search.trim() || !state.online
    || state.machines.some(machine => machine.projects?.length && machine.status !== 'connected'));
  scroller.setAttribute('aria-busy', Boolean(pending));
  $('refresh-list').classList.toggle('refreshing', Boolean(pending));
  $('refresh-list').dataset.tooltip = state.search.trim() && state.list.syncing ? t("正在同步搜索索引") : t("刷新会话");
  const scrollTop = scroller.scrollTop;
  if (project) scroller.innerHTML = view.projectSessionList(threads, state.selected, state.requests, state.sessionGroups, state.list.sources, pending, state.list.loadingMore);
  else {
    const groups = sessionSections(threads);
    const more = groups[2].threads.length > state.recentLimit || state.list.sources.some(source => !source.priority && source.cursor);
    groups[2].threads = groups[2].threads.slice(0, state.recentLimit);
    scroller.innerHTML = pending && !threads.length ? `<div class="list-placeholder">${state.list.syncing ? t("正在同步搜索索引…") : t("正在读取会话…")}</div>` : view.groupedSessionList(groups, state.selected, state.requests, more, state.list.loadingMore);
  }
  scroller.scrollTop = scrollTop;
  $('list-errors').innerHTML = state.errors.filter(error => !error.machineId || (project ? error.machineId === project.machineId : machineFilter.includes(error.machineId))).map(error => `<div class="list-error">${error.machineId ? `${view.h(machine(error.machineId)?.name || error.machineId)} · ` : ''}${view.h(error.message)}</div>`).join('');
  renderNavigation();
}

function refreshComposer() {
  const running = view.isRunning(state.detail) || conversation.turns.some(turn => ['inProgress', 'active'].includes(turn.status));
  goalPanel.render({ running, disabled: conversation.loading || state.sending || Boolean(state.editing?.saving) });
  const goalControls = conversation.goal && conversation.goal.status !== 'complete';
  const resumable = !goalControls && !running && conversation.turns.at(-1)?.status === 'interrupted';
  const attachments = state.attachments.get(selectedKey()) || [];
  const skills = state.skillInputs.get(selectedKey()) || [];
  $('new-thread-action').hidden = conversation.loading || !state.detail?.cwd
    || (!conversation.turns.length && !hasThreadDraft(selectedKey()));
  $('send-button').disabled = state.sending || state.uploadingFiles > 0 || state.editing?.saving || (!$('message-input').value.trim() && !attachments.length && !skills.length);
  $('attach-file').disabled = state.sending || state.uploadingFiles > 0;
  if (Boolean($('attach-file').querySelector('.spinner')) !== Boolean(state.uploadingFiles)) {
    $('attach-file').innerHTML = state.uploadingFiles ? '<span class="spinner"></span>' : icon('plus');
  }
  $('interrupt-button').hidden = !running;
  $('working-interrupt').hidden = !running;
  for (const id of ['resume-button', 'working-resume']) {
    $(id).hidden = !resumable;
    $(id).disabled = state.sending || state.editing?.saving || conversation.loading;
  }
  $('working-state').hidden = (!running && !resumable) || conversation.loading;
  $('working-state').classList.toggle('resumable', resumable);
  $('conversation').querySelectorAll('[data-question-item]').forEach(form => setQuestionBusy(form, state.sending || state.editing?.saving));
  $('activity-label').textContent = activityLabel();
  refreshTurnTimer();
  $('attachment-chips').hidden = !attachments.length && !skills.length;
  $('attachment-chips').innerHTML = attachments.map((attachment, index) => `<div class="attachment-chip">${attachment.preview ? `<button type="button" class="attachment-preview" data-preview-attachment="${index}" aria-label="${t("预览 {0}", view.h(attachment.name))}"><img src="${view.h(attachment.preview)}" alt="${view.h(attachment.name)}"></button>` : icon('file-code')}<span data-tooltip="${view.h(attachment.name)}">${view.h(view.basename(attachment.name))}</span><button type="button" data-remove-attachment="${index}" aria-label="${t("移除附件")}" data-i18n-aria-label="移除附件">${icon('x')}</button></div>`).join('')
    + skills.map((skill, index) => `<div class="attachment-chip">${icon('sparkles')}<span>${view.h(skill.name)}</span><button type="button" data-remove-skill="${index}" aria-label="${t("移除技能")}" data-i18n-aria-label="移除技能">${icon('x')}</button></div>`).join('');
  renderSessionControls();
}

function activityLabel() {
  if (state.detail?.status?.activeFlags?.includes('waitingOnApproval')) return t("等待审批");
  if (state.detail?.status?.activeFlags?.includes('waitingOnUserInput')) return t("等待回答");
  const turn = [...conversation.turns].reverse().find(turn => ['inProgress', 'active'].includes(turn.status));
  const item = [...(turn?.items || [])].reverse().find(item => item._active ?? (item.status === 'inProgress' || view.isCompactionRunning(item, true)));
  if (!item) return t("思考中");
  if (item.type === 'reasoning') return t("思考中");
  if (item.type === 'agentMessage') return t("正在回复");
  if (item.type === 'contextCompaction') return t("正在压缩");
  if (item.type === 'toolCallCompaction') return t("正在压缩 Tool call");
  if (item.type === 'commandExecution') return item.command ? t("执行 {0}", item.command) : t("执行命令");
  if (item.type === 'fileChange') return t("修改文件");
  if (item.type === 'webSearch') return item.query ? t("搜索 {0}", item.query) : t("搜索网页");
  if (item.type === 'collabAgentToolCall') return ({ wait: t('正在等待子代理'), spawnAgent: t('正在启动子代理'), resumeAgent: t('正在恢复子代理'), sendInput: t('正在向子代理发送输入'), closeAgent: t('正在关闭子代理') }[item.tool] || t('正在与子代理协作'));
  return item.progressMessage || `${item.server ? `${item.server} · ` : ''}${item.tool || item.name || t("执行工具")}`;
}

function refreshTurnTimer() {
  conversationRenderer.tick();
  const turn = [...conversation.turns].reverse().find(view.isTurnRunning);
  const duration = turn && view.turnDuration(turn);
  $('working-duration').textContent = duration == null ? '' : view.formatDuration(duration);
  $('working-duration').hidden = duration == null;
  const tpsEnabled = throughput.enabled(state.settings, turn);
  const realtime = tpsEnabled && state.online && machine(state.selected?.machineId)?.status === 'connected' && throughput.live(turn);
  const metric = tpsEnabled && (realtime || throughput.average(turn));
  $('working-tps').textContent = throughput.format(metric) || '— TPS';
  $('working-tps').dataset.tooltip = realtime ? t("实时回复速度（估算）") : metric ? t("本轮已测回复平均速度") : t("等待回复输出");
  $('working-tps').hidden = !turn || !tpsEnabled;
}

function renderSessionControls() {
  const model = state.settings.model;
  const effort = state.settings.reasoningEffort;
  updateSelectOptions($('composer-model'), composer.modelOptions(state.currentModels, model));
  updateSelectOptions($('composer-effort'), composer.effortOptions(state.currentModels, model, effort));
  setSettingsDisabled($('composer-model'), conversation.loading, state.settingsSaving);
  const ultra = effort === 'ultra';
  setSettingsDisabled($('composer-effort'), conversation.loading || ultra || !composer.reasoningEfforts(state.currentModels, model).length, state.settingsSaving);
  $('composer-ultra').hidden = !ultra && !composer.supportsUltra(state.currentModels, model);
  $('composer-ultra').classList.toggle('enabled', ultra);
  $('composer-ultra').setAttribute('aria-pressed', String(ultra));
  setSettingsDisabled($('composer-ultra'), conversation.loading || !state.currentModels.length, state.settingsSaving);
  composer.renderSpeedControl($('composer-fast'), state.currentModels, model, state.settings.serviceTier);
  setSettingsDisabled($('composer-fast'), false, state.settingsSaving);
  toolCompactControl($('composer-tool-compact'), state.settings, conversation.loading || !state.selected, state.settingsSaving);
  const permissions = { never: t("直接执行"), 'on-request': t("按需审批"), untrusted: t("不信任") };
  const permissionLabel = permissions[state.settings.approvalPolicy] || t("权限");
  if ($('permission-label').textContent !== permissionLabel) $('permission-label').textContent = permissionLabel;
  const compactions = conversation.compactions?.size ?? null;
  const contextSignature = JSON.stringify([locale(), state.tokenUsage, compactions]);
  if (contextSignature !== state.contextSignature) {
    state.contextSignature = contextSignature;
    const context = composer.contextInfo(state.tokenUsage);
    $('context-percent').textContent = context ? `${context.percent}%` : '—';
    $('context-ring').style.setProperty('--context-used', `${context?.percent || 0}%`);
    $('context-ring').classList.toggle('unknown', !context);
    $('context-popover').innerHTML = composer.contextContents(state.tokenUsage, compactions);
  }
}

function applySessionState(result) {
  if (result.thread) {
    state.detail = asThread({ ...state.detail, ...result.thread });
    updateThread(state.detail);
  }
  if (result.settings) {
    const settings = { ...result.settings };
    if ('effort' in settings && !('reasoningEffort' in settings)) settings.reasoningEffort = settings.effort;
    state.settings = { ...state.settings, ...settings };
    if (state.settings.reasoningEffort && state.settings.reasoningEffort !== 'ultra') {
      localStorage.setItem(effortStorageKey(state.settings.model), state.settings.reasoningEffort);
    }
  }
  if (result.tokenUsage || state.tokenUsage === null) state.tokenUsage = result.tokenUsage ?? state.tokenUsage;
  renderSessionControls();
  renderHeader();
}

async function refreshSessionState() {
  if (!state.selected) return;
  const key = selectedKey();
  const result = await api(`${endpoint()}/state`);
  if (key !== selectedKey()) return;
  applySessionState(result);
}

async function patchSettings(settings) {
  return actions.patchSettings(settings);
}

function renderHeader() {
  if (state.section === 'groups') return;
  if (!state.detail) return;
  const thread = state.detail;
  $('thread-title').textContent = view.titleOf(thread);
  $('thread-id').textContent = thread.id.slice(-6).toLowerCase();
  if (state.section === 'sessions') document.title = `${view.titleOf(thread)} · Nexus`;
  $('thread-compactions').hidden = conversation.compactions === null;
  $('thread-compactions').textContent = conversation.compactions === null ? '' : t("已压缩 {0} 次", conversation.compactions.size);
  const connection = machine(state.selected.machineId)?.status;
  $('thread-status').textContent = !state.online ? t("服务重连中") : connection === 'reconnecting' ? t("机器重连中") : state.selected.archived ? t("已归档") : view.statusLabel(thread);
  $('thread-status').className = `status-label ${view.isWaiting(thread) ? 'waiting' : view.isRunning(thread) ? 'running' : ''}`;
  $('archive-action').innerHTML = `${icon('archive')}<span>${state.selected.archived ? t("取消归档") : t("归档会话")}</span>`;
}

function renderConversation(forceBottom = false) {
  if (state.editing?.composing || $('conversation').querySelector('[data-question-form][data-composing]')) return;
  const scroller = $('conversation-scroll');
  const nearBottom = forceBottom || (!state.editing && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140);
  const scrollTop = scroller.scrollTop;
  const running = view.isRunning(state.detail) || conversation.turns.some(view.isTurnRunning);
  conversationRenderer.render(conversation.loading ? [] : conversation.visibleTurns, state.threads, {
    settings: state.settings,
    editing: state.editing, busy: state.sending, running, machineId: state.selected?.machineId, cwd: state.detail?.cwd,
  });
  $('history-loading').hidden = !conversation.loading;
  $('older-history').hidden = !conversation.cursor || conversation.loading;
  refreshComposer();
  renderHeader();
  if (nearBottom) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
  else scroller.scrollTop = scrollTop;
}

function scheduleConversation() {
  if (renderFrame) return;
  renderFrame = requestAnimationFrame(() => { renderFrame = null; renderConversation(); });
}

function renderRequests() {
  const requests = state.selected ? state.requests.filter(request => request.machineId === state.selected.machineId && request.threadId === state.selected.threadId) : [];
  const signature = JSON.stringify([locale(), requests]);
  if (signature === state.requestSignature) return;
  if ($('pending-requests').querySelector('[data-question-form][data-composing]')) return;
  state.requestSignature = signature;
  renderRequestCards($('pending-requests'), requests);
  enhanceSelects($('pending-requests'));
}

function renderTasks() {
  $('task-panel').hidden = !state.tasksOpen;
  document.querySelectorAll('[data-thread-action="tasks"]').forEach(button => {
    button.classList.toggle('active', state.tasksOpen);
    button.setAttribute('aria-expanded', String(state.tasksOpen));
  });
  $('task-list').innerHTML = state.tasksLoading ? '<div class="task-empty"><span class="spinner"></span></div>' : state.tasksError ? `<div class="task-empty">${view.h(state.tasksError)}</div>` : view.backgroundTasks(state.tasks);
  $('clean-tasks').disabled = state.tasksLoading || !state.tasks.length;
  $('toggle-tasks').dataset.tooltip = state.tasks.length ? t("后台任务 · {0}", state.tasks.length) : t("后台任务");
}

async function refreshMachines() {
  const result = await api('/machines');
  const projectsChanged = state.machines.length && result.machines.some(updated => {
    const before = machine(updated.id)?.projects || [];
    const after = updated.projects || [];
    return before.length !== after.length || before.some(cwd => !after.includes(cwd));
  });
  state.machines = result.machines;
  state.machinesLoaded = true;
  terminalBrowser.setMachines(state.machines);
  machineFilter.update(state.machines);
  document.querySelector('[data-filter="files"]').disabled = !state.machines.length;
  const project = currentProject();
  if (project && !machine(project.machineId)?.projects?.includes(project.cwd)) {
    state.filter = 'sessions';
    state.recentLimit = 10;
    state.sessionGroups = { unarchived: true, archived: false };
    state.errors = [];
  }
  if (state.selected && state.detail?.cwd && !trackedThread(asThread(state.detail))) {
    saveDraft();
    clearSelection();
    localStorage.removeItem('nexus-selected');
    history.replaceState(null, '', location.pathname + location.search);
  }
  if (projectsChanged) {
    state.list = { sources: [], loading: false, loadingMore: false };
    scheduleListRefresh();
  }
  renderList();
  if ($('modal').open && $('machines-list')) $('machines-list').innerHTML = view.machineCards(state.machines, state.machineActions);
}

function sourcePage(source, cursor, limit) {
  return api(source.search ? `/search${searchParams(source, cursor, limit)}`
    : `/machines/${encodeURIComponent(source.machineId)}/threads${query({ scope: 'history', cwd: currentProject()?.cwd, archived: source.archived, cursor, limit })}`);
}

function otherCount(source) {
  const live = new Map(state.liveThreads.map(thread => [view.threadKey(thread), thread]));
  return source.data.filter(thread => {
    const current = source.search ? thread : live.get(view.threadKey(thread)) || thread;
    return !waitingForThread(current) && !view.isRunning(current);
  }).length;
}

async function fillSource(source, list, limit) {
  while (source.cursor && (source.priority || otherCount(source) < limit)) {
    const result = await sourcePage(source, source.cursor, 50);
    if (list !== state.list) return;
    source.data = mergeThreads(source.data, result.data);
    source.cursor = result.nextCursor;
    source.paged = true;
    list.syncing ||= result.syncing;
    result.data.forEach(({ searchMatch, ...thread }) => updateThread(thread, { live: false }));
    renderList();
  }
}

async function refreshThreads() {
  if (state.search.trim()) return refreshSearch();
  const list = state.list;
  if (list.loading || list.loadingMore) return;
  list.loading = true;
  renderList();
  const project = currentProject();
  const machines = project ? [machine(project.machineId)] : machineFilter.selectedMachines();
  const limit = project ? 50 : 10;
  const sources = machines.flatMap(machine => [false, true].map(archived =>
    list.sources.find(source => source.machineId === machine.id && source.archived === archived)
    || { machineId: machine.id, archived, data: [], cursor: null, paged: false }));
  list.sources = sources;
  try {
    const errors = [];
    const jobs = [
      api('/threads').then(result => {
        if (list !== state.list) return;
        errors.push(...(result.errors || []));
        state.liveThreads = result.data;
        result.data.forEach(thread => updateThread(thread));
        renderList();
      }),
      ...sources.map(async source => {
        const result = await sourcePage(source, null, limit);
        if (list !== state.list) return;
        errors.push(...(result.errors || []));
        source.data = source.paged ? mergeThreads(source.data, result.data) : result.data;
        source.data = source.data.filter(thread => Boolean(thread.archived) === source.archived);
        if (!source.paged) source.cursor = result.nextCursor;
        result.data.forEach(thread => updateThread(thread, { live: false }));
        renderList();
        if (!project) await fillSource(source, list, state.recentLimit);
      }),
    ];
    await Promise.all(jobs.map(async (job, index) => {
      try { await job; }
      catch (error) { errors.push({ machineId: sources[index - 1]?.machineId || '', message: error.message }); }
      if (list !== state.list) return;
      state.errors = [...new Map(errors.map(error => [`${error.machineId}:${error.message}`, error])).values()];
      renderList();
    }));
  } finally {
    list.loading = false;
    if (list === state.list) { $('refresh-list').classList.remove('refreshing'); renderList(); }
  }
}

function searchParams(source, cursor, limit) {
  const project = listProject();
  return query({ searchTerm: state.search.trim(), mode: state.searchMode,
    view: source.view || 'recent', machineId: source.machineId, cwd: project?.cwd,
    archived: source.archived, cursor, limit });
}

async function refreshSearch() {
  const list = state.list;
  if (list.loading || list.loadingMore) return;
  list.loading = true;
  renderList();
  const project = listProject();
  const machines = project ? [machine(project.machineId)] : machineFilter.selectedMachines();
  const sources = machines.flatMap(machine => project
    ? [false, true].map(archived => ({ machineId: machine.id, archived, search: true, data: [], cursor: null }))
    : ['waiting', 'working', 'recent'].map(view => ({ machineId: machine.id, view, priority: view !== 'recent', search: true, data: [], cursor: null })));
  try {
    const results = await Promise.all(sources.map(source => {
      const previous = list.sources.find(item => item.machineId === source.machineId && item.archived === source.archived && item.view === source.view);
      return sourcePage(source, null, Math.max(project || source.priority ? 50 : state.recentLimit, previous?.data.length || 0));
    }));
    if (list !== state.list) return;
    list.sources = sources.map((source, index) => ({ ...source, data: results[index].data, cursor: results[index].nextCursor }));
    list.syncing = results.some(result => result.syncing);
    state.errors = [...new Map(results.flatMap(result => result.errors || []).map(error => [`${error.machineId}:${error.message}`, error])).values()];
    for (const result of results) for (const { searchMatch, ...thread } of result.data) updateThread(thread, { live: false });
    if (!project) {
      await Promise.all(list.sources.map(source => fillSource(source, list, state.recentLimit)));
    }
  } finally {
    list.loading = false;
    list.searchPending = false;
    if (list === state.list) renderList();
  }
}

async function refreshRequests() {
  const result = await api('/requests');
  state.requests = result.data;
  sidePanel.setRequests(state.requests);
  renderRequests();
  renderList();
}

async function refreshAll() {
  await refreshMachines().catch(error => toast(error.message, true));
  const results = await Promise.allSettled([refreshThreads(), refreshRequests(), terminalBrowser.refresh()]);
  results.forEach(result => { if (result.status === 'rejected') toast(result.reason.message, true); });
}

function scheduleListRefresh() {
  clearTimeout(listRefreshTimer);
  listRefreshTimer = setTimeout(() => refreshThreads().catch(error => toast(error.message, true)), 350);
}

async function loadMoreThreads(group = 'recent') {
  const list = state.list;
  if (list.loading || list.loadingMore) return;
  const project = listProject();
  list.loadingMore = true;
  renderList();
  try {
    if (list !== state.list) return;
    const sources = list.sources.filter(source => !source.priority && source.cursor && (group === 'recent' || source.archived === (group === 'archived')));
    const more = !project && sessionSections()[2].threads.length > state.recentLimit;
    if (!sources.length && !more) return;
    list.loading = true;
    const results = await Promise.allSettled(sources.map(source => project ? sourcePage(source, source.cursor, 50) : fillSource(source, list, state.recentLimit + 10)));
    if (list !== state.list) return;
    for (const [index, outcome] of results.entries()) {
      if (outcome.status === 'rejected') { toast(outcome.reason.message, true); continue; }
      if (!project) continue;
      const source = sources[index];
      source.data = mergeThreads(source.data, outcome.value.data);
      source.cursor = outcome.value.nextCursor;
      source.paged = true;
      outcome.value.data.forEach(({ searchMatch, ...thread }) => updateThread(thread, { live: false }));
    }
    if (!project) state.recentLimit += 10;
  } finally { list.loading = false; list.loadingMore = false; if (list === state.list) renderList(); }
}

function clearSelection() {
  sidePanel.setParent(null);
  developerPanel.select(null);
  contextPanel.select(null);
  closeSelectMenu();
  state.selected = null;
  state.detail = null;
  conversation.select(null);
  state.editing = null;
  state.settings = {};
  state.tokenUsage = null;
  closeSlashMenu();
  $('thread-view').hidden = true;
  $('app').classList.remove('mobile-detail');
  document.title = 'Nexus';
  renderList();
}

function saveDraft() {
  actions.saveDraft();
}

function hasThreadDraft(key) {
  return Boolean(localStorage.getItem(draftStorageKey(key))
    || state.attachments.get(key)?.length || state.skillInputs.get(key)?.length
    || [...conversation.submissions.values()].some(submission => submission.key === key)
    || state.uploadingFiles || (key === selectedKey() && $('message-input').value));
}

async function selectThread(machineId, threadId, { archived = Boolean(state.threads.find(thread => thread.machineId === machineId && thread.id === threadId)?.archived), updateHash = true } = {}) {
  showSessions({ updateHash: false });
  closeSelectMenu();
  const selection = conversation.select({ machineId, threadId });
  state.selected = { machineId, threadId, archived };
  sidePanel.setParent({ machineId, threadId }).catch(error => sidePanel.error(error.message));
  developerPanel.select({ machineId, threadId });
  contextPanel.select({ machineId, threadId });
  state.editing = null;
  state.detail = { ...(state.threads.find(thread => thread.machineId === machineId && thread.id === threadId) || { id: threadId, machineId, machineName: machine(machineId)?.name, status: { type: 'notLoaded' } }), archived };
  state.settings = {};
  state.tokenUsage = null;
  state.currentModels = state.models.get(machineId) || [];
  state.tasks = [];
  state.requestSignature = '';
  closeSlashMenu();
  if (updateHash) history.replaceState(null, '', `#${encodeURIComponent(machineId)}/${encodeURIComponent(threadId)}`);
  localStorage.setItem('nexus-selected', JSON.stringify(state.selected));
  $('welcome').hidden = true;
  $('thread-view').hidden = false;
  $('app').classList.add('mobile-detail');
  $('message-input').value = localStorage.getItem(draftStorageKey(selectedKey())) || '';
  resizeComposer();
  showThreadError();
  renderList();
  renderConversation(true);
  renderRequests();
  hideNavigation();
  loadModels(machineId).catch(() => {});
  try {
    await conversation.refresh();
  } catch (error) {
    if (selection === conversation.selection) showThreadError(error.message);
  }
}

async function loadOlderHistory() {
  const scroll = $('conversation-scroll');
  const oldHeight = scroll.scrollHeight;
  $('older-history').disabled = true;
  try {
    if (await conversation.older()) requestAnimationFrame(() => { scroll.scrollTop += scroll.scrollHeight - oldHeight; });
  } finally { $('older-history').disabled = false; }
}

async function loadModels(machineId) {
  if (!state.models.has(machineId)) state.models.set(machineId, (await api(`/machines/${encodeURIComponent(machineId)}/models`)).data);
  if (state.selected?.machineId !== machineId) return;
  state.currentModels = state.models.get(machineId);
  renderSessionControls();
  sidePanel.renderControls();
}

async function setModel() {
  const model = $('composer-model').value;
  if (model) await patchSettings({ model });
}

async function setEffort() {
  const effort = $('composer-effort').value;
  if (effort) await patchSettings({ effort });
}

async function toggleFast() {
  await patchSettings({ serviceTier: composer.nextSpeedTier(state.currentModels, state.settings.model, state.settings.serviceTier) });
}

async function toggleUltra() {
  const model = state.settings.model;
  const enabled = state.settings.reasoningEffort === 'ultra';
  if (!enabled && !composer.supportsUltra(state.currentModels, model)) return;
  const effort = enabled ? composer.standardEffort(state.currentModels, model, savedEffort(model)) : 'ultra';
  await patchSettings({ effort });
}

function refreshEffortField(models, model, effortId, ultraId, { effort, includeDefault = false, defaultEffort } = {}) {
  const select = $(effortId);
  const toggle = $(ultraId);
  toggle.disabled = !composer.supportsUltra(models, model);
  if (toggle.disabled) toggle.checked = false;
  if (effort !== undefined) select.dataset.standardEffort = effort;
  const standard = includeDefault && !select.dataset.standardEffort ? '' : composer.standardEffort(models, model, select.dataset.standardEffort);
  select.innerHTML = composer.effortOptions(models, model, toggle.checked ? 'ultra' : standard, { includeDefault, defaultEffort });
  select.disabled = toggle.checked;
  select.onchange = () => { select.dataset.standardEffort = select.value; };
}

function resizeComposer() {
  const input = $('message-input');
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
  refreshComposer();
  updateSlashMenu();
}

async function sendMessage(event) {
  event?.preventDefault();
  if (state.editing?.saving) return;
  const text = $('message-input').value.trim();
  if (text === '/' && state.slashCommands.length) return executeSlash(state.slashCommands[state.slashIndex].name);
  if (/^\/[a-z-]+(?:\s|$)/i.test(text)) {
    const [, name, args = ''] = text.match(/^\/([a-z-]+)(?:\s+([\s\S]*))?$/i);
    const command = composer.slashCommands().find(command => command.name === name.toLowerCase());
    if (command) return executeSlash(command.name, args);
    toast(t("未知命令 /{0}", name), true);
    return;
  }
  const selection = conversation.selection;
  try { await actions.send(); }
  catch (error) { if (selection === conversation.selection) showThreadError(error.message); }
}

async function interruptTurn() {
  await actions.interrupt();
  toast(t("已请求中断"));
  scheduleListRefresh();
}

function resumeTurn() { return actions.resume(); }
function editMessage(itemId, turnId, options) {
  const editing = actions.editMessage(itemId, turnId, options);
  if (!editing || options?.regenerate) return editing;
  const input = $('conversation').querySelector('.message-edit-input');
  input.style.height = `${Math.min(input.scrollHeight, 340)}px`;
  input.focus({ preventScroll: true });
  input.setSelectionRange(input.value.length, input.value.length);
  input.scrollIntoView({ block: 'nearest' });
}
function cancelEdit() { actions.cancelEdit(); }
function saveEditedMessage(event) { event?.preventDefault(); return actions.saveEditedMessage(); }
function regenerateTurn(turnId) { return actions.regenerateTurn(turnId); }
function regenerateMessage(itemId, turnId) { return actions.regenerateMessage(itemId, turnId); }

async function forkThread(lastTurnId, lastItemId) {
  const ref = { ...state.selected };
  const body = lastItemId ? { turnId: lastTurnId, lastItemId } : { lastTurnId };
  const result = await api(`${endpoint()}/fork`, { method: 'POST', body, timeout: 60_000 });
  toast(t("已创建分叉会话"));
  await selectThread(ref.machineId, result.thread.id, { archived: false });
  scheduleListRefresh();
}

async function refreshTasks() {
  if (!state.selected) return;
  const key = selectedKey();
  state.tasksLoading = true;
  state.tasksError = '';
  renderTasks();
  try {
    const result = await api(`${endpoint()}/background-tasks`);
    if (key === selectedKey()) state.tasks = result.data;
  } catch (error) {
    state.tasksError = error.message;
    throw error;
  } finally { state.tasksLoading = false; renderTasks(); }
}

async function stopTask(processId) {
  await api(`${endpoint()}/background-tasks/${encodeURIComponent(processId)}/terminate`, { method: 'POST', body: {} });
  await refreshTasks();
  toast(t("后台任务已停止"));
}

async function cleanTasks() {
  await api(`${endpoint()}/background-tasks/clean`, { method: 'POST', body: {} });
  await refreshTasks();
  toast(t("已请求停止全部后台任务"));
}

function openModal({ title, body, submitLabel = t('保存'), onSubmit, afterRender, footer = true }) {
  closeSelectMenu();
  closeSlashMenu();
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = body;
  $('modal-error').hidden = true;
  $('modal-error').classList.remove('neutral');
  $('modal-error').setAttribute('role', 'alert');
  $('modal-submit').textContent = submitLabel;
  $('modal-submit').disabled = false;
  $('modal-footer').hidden = !footer;
  state.modalHandler = onSubmit;
  if (!$('modal').open) $('modal').showModal();
  raiseToasts();
  afterRender?.();
  enhanceSelects($('modal-body'));
}

function closeModal() {
  closeSelectMenu();
  $('modal').close();
  state.modalHandler = null;
}

function machineForm(machineInfo) {
  const editing = Boolean(machineInfo);
  const info = machineInfo || { transport: 'ssh', port: 22, codexBin: 'codex', connection: 'daemon' };
  const ssh = info.transport === 'ssh';
  openModal({
    title: editing ? t("编辑机器") : t("连接一台机器"),
    submitLabel: editing ? t("保存") : t("添加并连接"),
    body: `<label class="field"><span data-i18n="名称">${t("名称")}</span><input name="name" value="${view.h(info.name)}" placeholder="${t("例如 devbox")}" required autocomplete="off" data-i18n-placeholder="例如 devbox"></label>${ssh ? `<label class="field"><span data-i18n="主机地址">${t("主机地址")}</span><input name="host" value="${view.h(info.host)}" placeholder="${t("IP、域名或 SSH 别名")}" required autocomplete="off" data-i18n-placeholder="IP、域名或 SSH 别名"></label><div class="field-row"><label class="field"><span data-i18n="SSH 用户">${t("SSH 用户")}</span><input name="user" value="${view.h(info.user)}" placeholder="${t("沿用 SSH 配置")}" autocomplete="off" data-i18n-placeholder="沿用 SSH 配置"></label><label class="field"><span data-i18n="端口">${t("端口")}</span><input name="port" type="number" value="${info.port || 22}"></label></div>` : ''}<details class="advanced-fields"><summary data-i18n="更多设置">${t("更多设置")}</summary><label class="field"><span data-i18n="Codex 命令">${t("Codex 命令")}</span><input name="codexBin" value="${view.h(info.codexBin || 'codex')}"></label><label class="field"><span data-i18n="运行方式">${t("运行方式")}</span><select name="connection"><option value="daemon" ${info.connection !== 'spawn' ? 'selected' : ''} data-i18n="连接共享后台服务">${t("连接共享后台服务")}</option><option value="spawn" ${info.connection === 'spawn' ? 'selected' : ''} data-i18n="启动独立运行实例">${t("启动独立运行实例")}</option></select></label></details>`,
    onSubmit: async form => {
      const body = { ...Object.fromEntries(form), transport: info.transport };
      if (ssh) body.port = Number(body.port || 22);
      const result = await api(editing ? `/machines/${encodeURIComponent(info.id)}` : '/machines', { method: editing ? 'PATCH' : 'POST', body });
      closeModal();
      await refreshMachines();
      toast(editing ? t("机器配置已保存") : t("机器已添加，正在连接"));
      try { await api(`/machines/${encodeURIComponent(result.id || result.machine?.id)}/connect`, { method: 'POST', body: {}, timeout: 60_000 }); }
      catch (error) { toast(error.message, true); }
      await refreshAll();
    },
  });
}

function findMachineThreads(machineInfo) {
  let found = [];
  let cursor = null;
  let cwd = '';
  const directories = [...new Set([
    ...(machineInfo.projects || []),
    ...state.threads.filter(thread => thread.machineId === machineInfo.id).map(thread => thread.cwd),
  ].filter(Boolean))];
  const load = async () => {
    const result = await api(`/machines/${encodeURIComponent(machineInfo.id)}/threads${query({ scope: 'history', cwd, cursor, limit: 30 })}`);
    found.push(...result.data);
    cursor = result.nextCursor;
    $('found-threads').innerHTML = found.length ? found.map(thread => `<button type="button" class="session-row" data-connect-found="${view.h(thread.id)}"><div class="session-title-row"><span class="session-title">${view.h(view.titleOf(thread))}</span><span class="text-button" data-i18n="接入">${t("接入")}</span></div><div class="session-meta"><span class="session-context">${view.h(thread.cwd)}</span><time class="session-time">${view.h(view.timeAgo(view.threadTime(thread)))}</time></div></button>`).join('') : `<div class="list-empty" data-i18n="暂无会话">${t("暂无会话")}</div>`;
    $('found-more').hidden = !cursor;
  };
  openModal({
    title: t("{0} · 查找会话", machineInfo.name), submitLabel: t("查找"),
    body: `<label class="field"><span data-i18n="工作目录">${t("工作目录")}</span><input name="cwd" list="history-directories" placeholder="${t("执行机器上的项目目录")}" required autocomplete="off" data-i18n-placeholder="执行机器上的项目目录"><datalist id="history-directories">${directories.map(path => `<option value="${view.h(path)}">`).join('')}</datalist></label><div id="found-threads"></div><button type="button" class="text-button" id="found-more" hidden data-i18n="加载更多">${t("加载更多")}</button>`,
    onSubmit: async form => {
      cwd = form.get('cwd').trim();
      found = [];
      cursor = null;
      await api(`/machines/${encodeURIComponent(machineInfo.id)}/projects`, { method: 'POST', body: { cwd } });
      await api(`/machines/${encodeURIComponent(machineInfo.id)}/connect`, { method: 'POST', body: {}, timeout: 60_000 });
      await load();
    },
    afterRender: () => {
      $('found-more').addEventListener('click', run(load));
      $('found-threads').addEventListener('click', run(async event => {
        const button = event.target.closest('[data-connect-found]');
        if (!button) return;
        const id = button.dataset.connectFound;
        closeModal();
        await changeFilter('sessions');
        await selectThread(machineInfo.id, id, { archived: Boolean(found.find(thread => thread.id === id)?.archived) });
      }));
    },
  });
}

function machinesModal() {
  openModal({
    title: t("机器"), footer: false,
    body: `<div id="machines-list">${view.machineCards(state.machines, state.machineActions)}</div><button type="button" class="button subtle settings-add" data-add-machine>${t("{0}添加机器", icon('plus'))}</button>`,
  });
}

function settingsModal(tab = 'appearance') {
  openModal({
    title: t("设置"), footer: false,
    body: `<div class="settings-tabs"><button type="button" class="${tab === 'appearance' ? 'active' : ''}" data-settings-tab="appearance" data-i18n="通用">${t("通用")}</button><button type="button" class="${tab === 'features' ? 'active' : ''}" data-settings-tab="features" data-i18n="功能">${t("功能")}</button><button type="button" class="${tab === 'about' ? 'active' : ''}" data-settings-tab="about" data-i18n="关于">${t("关于")}</button></div>${tab === 'appearance' ? `<label class="field"><span data-i18n="语言">${t("语言")}</span><select id="language-select" aria-label="${t("语言")}" data-i18n-aria-label="语言"><option value="en" lang="en">English</option><option value="zh-CN" lang="zh-CN">简体中文</option></select></label><label class="field"><span data-i18n="外观">${t("外观")}</span><select id="theme-select"><option value="system" data-i18n="跟随系统">${t("跟随系统")}</option><option value="light" data-i18n="浅色">${t("浅色")}</option><option value="dark" data-i18n="深色">${t("深色")}</option></select></label><label class="field"><span data-i18n="终端外观">${t("终端外观")}</span><select id="terminal-theme-select"><option value="system" data-i18n="跟随系统">${t("跟随系统")}</option><option value="dark" data-i18n="深色">${t("深色")}</option><option value="light" data-i18n="浅色">${t("浅色")}</option><option value="client" data-i18n="跟随客户端">${t("跟随客户端")}</option></select></label><div class="install-section"><button type="button" class="button subtle" data-install-app>${icon('download')}${installLabel()}</button></div><div class="notification-settings"><button type="button" class="button subtle" id="toggle-notifications" disabled>${t("{0}读取通知设置…", icon('bell'))}</button><button type="button" class="button subtle" id="test-notification" hidden>${t("测试通知")}</button></div>` : tab === 'features' ? featuresPanel(state.config.features) : `<p class="settings-note">Nexus ${view.h(state.config.version || '')}</p>`}`,
    afterRender: () => {
      if ($('language-select')) {
        $('language-select').value = locale();
        $('language-select').onchange = event => setLanguage(event.target.value);
      }
      if ($('theme-select')) {
        $('theme-select').value = document.documentElement.dataset.theme || 'system';
        $('theme-select').onchange = event => setTheme(event.target.value);
      }
      if ($('terminal-theme-select')) {
        $('terminal-theme-select').value = document.documentElement.dataset.terminalTheme || 'client';
        $('terminal-theme-select').onchange = event => {
          document.documentElement.dataset.terminalTheme = event.target.value;
          localStorage.setItem('nexus-terminal-theme', event.target.value);
        };
      }
      if (tab === 'appearance') refreshNotificationSettings().catch(error => toast(error.message, true));
      if (tab === 'features') bindFeatures(state.config);
    },
  });
}

function addProjectModal(machineId) {
  const target = machine(machineId);
  const project = currentProject();
  const initialPath = project?.machineId === machineId ? project.cwd : target.projects?.[0] || '~';
  let navigation = 0;
  let creationPath = null;
  const resetCreation = () => {
    creationPath = null;
    $('modal-submit').textContent = t("加入项目");
    $('modal-error').hidden = true;
    $('modal-error').classList.remove('neutral');
    $('modal-error').setAttribute('role', 'alert');
  };
  const offerCreation = cwd => {
    creationPath = cwd;
    $('modal-submit').textContent = t("创建并加入");
    $('modal-error').classList.add('neutral');
    $('modal-error').setAttribute('role', 'status');
    $('modal-error').textContent = t("目录不存在，是否创建？");
    $('modal-error').hidden = false;
  };
  const browse = async path => {
    const generation = ++navigation;
    const input = $('project-directory');
    input.value = path;
    const draft = input.value;
    $('directory-list').innerHTML = '<div class="list-placeholder"><span class="spinner"></span></div>';
    resetCreation();
    try {
      const result = await api(`/machines/${encodeURIComponent(machineId)}/directories${query({ path })}`);
      if (!input.isConnected || generation !== navigation) return;
      if (input.value === draft) input.value = result.path;
      $('directory-parent').dataset.directory = result.parent;
      $('directory-parent').disabled = result.path === result.parent;
      $('directory-refresh').dataset.directory = result.path;
      let current = '';
      $('directory-breadcrumbs').innerHTML = `<button type="button" class="text-button" data-directory="/">/</button>` + result.path.split('/').filter(Boolean).map(part => {
        current += `/${part}`;
        return `${icon('chevron-right')}<button type="button" class="text-button" data-directory="${view.h(current)}">${view.h(part)}</button>`;
      }).join('');
      $('directory-list').innerHTML = result.entries.length ? result.entries.map(entry => `<button type="button" class="nav-row directory-row" data-directory="${view.h(entry.path)}">${icon('folder')}<span>${view.h(entry.name)}</span>${icon('chevron-right')}</button>`).join('') : `<div class="list-empty" data-i18n="暂无子目录">${t("暂无子目录")}</div>`;
    } catch (error) {
      if (!input.isConnected || generation !== navigation) return;
      $('directory-list').innerHTML = '';
      if (error.detail?.code === 'directory_not_found' && input.value.trim() === path) {
        offerCreation(path);
        return;
      }
      $('modal-error').textContent = error.message;
      $('modal-error').hidden = false;
    }
  };
  openModal({
    title: t("{0} · 加入项目", target.name), submitLabel: t("加入项目"),
    body: `<label class="field"><span data-i18n="项目目录">${t("项目目录")}</span><div class="directory-path"><input id="project-directory" name="cwd" value="${view.h(initialPath)}" placeholder="${t("执行机器上的项目目录")}" required autocomplete="off" autofocus data-i18n-placeholder="执行机器上的项目目录"><button type="button" class="icon-button" id="directory-open" aria-label="${t("打开目录")}" data-tooltip="${t("打开目录")}" data-i18n-aria-label="打开目录" data-i18n-data-tooltip="打开目录">${icon('arrow-right')}</button></div></label><div class="directory-browser"><div class="directory-toolbar"><button type="button" class="icon-button small" id="directory-parent" aria-label="${t("上一级目录")}" data-tooltip="${t("上一级目录")}" disabled data-i18n-aria-label="上一级目录" data-i18n-data-tooltip="上一级目录">${icon('arrow-up')}</button><nav class="directory-breadcrumbs" id="directory-breadcrumbs" aria-label="${t("当前目录")}" data-i18n-aria-label="当前目录"></nav><button type="button" class="icon-button small" id="directory-refresh" aria-label="${t("刷新目录")}" data-tooltip="${t("刷新目录")}" data-i18n-aria-label="刷新目录" data-i18n-data-tooltip="刷新目录">${icon('refresh')}</button></div><div class="directory-list" id="directory-list"></div></div>`,
    afterRender: () => {
      $('project-directory').addEventListener('input', resetCreation);
      $('directory-open').addEventListener('click', () => browse($('project-directory').value.trim()));
      $('modal-body').querySelector('.directory-browser').addEventListener('click', event => {
        const button = event.target.closest('[data-directory]');
        if (button) browse(button.dataset.directory);
      });
      browse(initialPath);
    },
    onSubmit: async form => {
      const cwd = form.get('cwd').trim();
      const createDirectory = cwd === creationPath;
      let result;
      try {
        result = await api(`/machines/${encodeURIComponent(machineId)}/projects`, { method: 'POST', body: { cwd, createDirectory } });
      } catch (error) {
        if (!createDirectory && error.detail?.code === 'directory_not_found') {
          offerCreation(cwd);
          return false;
        }
        resetCreation();
        throw error;
      }
      state.machines = state.machines.map(machine => machine.id === machineId ? result : machine);
      state.collapsedMachines.delete(machineId);
      localStorage.setItem('nexus-collapsed-machines', JSON.stringify([...state.collapsedMachines]));
      closeModal();
      await changeFilter(view.projectFilter(machineId, cwd));
    },
  });
}

async function removeProject(button) {
  const { machineId, cwd } = view.projectFromFilter(button.dataset.removeProject);
  const path = `/machines/${encodeURIComponent(machineId)}/projects`;
  button.disabled = true;
  try {
    await api(path, { method: 'DELETE', body: { cwd } });
    await refreshMachines();
    toast(t("已移出追踪：{0}", view.basename(cwd)), false, {
      label: t("撤销"),
      onClick: async () => {
        await api(path, { method: 'POST', body: { cwd } });
        await refreshMachines();
        await refreshThreads();
      },
    });
  } finally { button.disabled = false; }
}

async function createProjectThread({ machineId, cwd }, button) {
  button.disabled = true;
  try {
    const result = await api(`/machines/${encodeURIComponent(machineId)}/threads`, { method: 'POST', body: { cwd } });
    updateThread({ ...result.thread, machineId, machineName: machine(machineId)?.name });
    saveDraft();
    await selectThread(machineId, result.thread.id, { archived: false });
    scheduleListRefresh();
    $('message-input').focus();
  } finally { button.disabled = false; }
}

async function newProjectThread() {
  const project = currentProject();
  if (project) await createProjectThread(project, $('project-new-thread'));
}

async function newThreadModal({ machineId, cwd } = {}) {
  if (!state.machines.length) { machineForm(); return; }
  const project = currentProject();
  const initialMachine = machine(machineId || project?.machineId) || machine(state.selected?.machineId) || state.machines.find(machine => machine.status === 'connected') || state.machines[0];
  const initialCwd = cwd ?? (project?.machineId === initialMachine.id ? project.cwd : initialMachine.projects?.[0] || '');
  openModal({
    title: t("新会话"), submitLabel: t("创建会话"),
    body: `<label class="field"><span data-i18n="执行机器">${t("执行机器")}</span><select name="machineId" id="new-machine">${state.machines.map(machine => `<option value="${view.h(machine.id)}" ${machine.id === initialMachine.id ? 'selected' : ''}>${view.h(machine.name)}${machine.status === 'connected' ? '' : t(" · 未连接")}</option>`).join('')}</select></label><label class="field"><span data-i18n="工作目录">${t("工作目录")}</span><input name="cwd" id="new-cwd" value="${view.h(initialCwd)}" list="project-paths" placeholder="${t("执行机器上的项目目录")}" data-i18n-placeholder="执行机器上的项目目录"><datalist id="project-paths">${(initialMachine.projects || []).map(path => `<option value="${view.h(path)}">`).join('')}</datalist></label><label class="field"><span data-i18n="模型">${t("模型")}</span><select name="model" id="new-model"><option value="" data-i18n="默认">${t("默认")}</option></select></label><label class="field"><span data-i18n="推理强度">${t("推理强度")}</span><select name="effort" id="new-effort"><option value="" data-i18n="默认">${t("默认")}</option></select></label>${composer.ultraField('new-ultra')}<details class="advanced-fields"><summary data-i18n="权限设置">${t("权限设置")}</summary><label class="field"><span data-i18n="文件访问">${t("文件访问")}</span><select name="sandbox"><option value="" data-i18n="沿用远端设置">${t("沿用远端设置")}</option><option value="read-only" data-i18n="只读">${t("只读")}</option><option value="workspace-write" data-i18n="工作目录可写">${t("工作目录可写")}</option><option value="danger-full-access" data-i18n="完整访问">${t("完整访问")}</option></select></label><label class="field"><span data-i18n="执行审批">${t("执行审批")}</span><select name="approvalPolicy"><option value="" data-i18n="沿用远端设置">${t("沿用远端设置")}</option><option value="on-request" data-i18n="按需请求">${t("按需请求")}</option><option value="never" data-i18n="直接执行">${t("直接执行")}</option></select></label></details>`,
    afterRender: () => {
      const machineSelect = $('new-machine');
      const defaultsRequest = api('/session-defaults');
      let defaults = { models: {} };
      const fillEfforts = () => {
        const models = state.models.get(machineSelect.value) || [];
        const selected = $('new-model').value;
        const sharedModel = models.find(model => composer.modelId(model) === defaults.model);
        const model = selected || (sharedModel ? defaults.model : composer.modelId(models.find(model => model.isDefault) || {}));
        const preferred = selected || sharedModel ? defaults.models[model]?.effort : undefined;
        refreshEffortField(models, model, 'new-effort', 'new-ultra', {
          includeDefault: true, defaultEffort: composer.supportedEffort(models, model, preferred),
        });
      };
      const fillModels = async id => {
        try {
          const [saved, models] = await Promise.all([
            defaultsRequest,
            state.models.get(id) || api(`/machines/${encodeURIComponent(id)}/models`).then(result => result.data),
          ]);
          state.models.set(id, models);
          if (!machineSelect.isConnected || !$('modal').open || machineSelect.value !== id) return;
          defaults = saved;
          const preset = models.find(model => composer.modelId(model) === defaults.model);
          const label = preset ? t("{0} · 默认", preset.displayName || defaults.model) : t("默认");
          $('new-model').innerHTML = composer.modelOptions(models, '', label);
          $('new-effort').dataset.standardEffort = '';
          fillEfforts();
        } catch (error) {
          if (!machineSelect.isConnected || !$('modal').open || machineSelect.value !== id) return;
          $('modal-error').textContent = error.message;
          $('modal-error').hidden = false;
        }
      };
      $('new-model').onchange = () => { $('new-effort').dataset.standardEffort = ''; fillEfforts(); };
      $('new-ultra').onchange = fillEfforts;
      machineSelect.onchange = event => {
        const current = machine(event.target.value);
        $('new-cwd').value = current.projects?.[0] || '';
        $('project-paths').innerHTML = (current.projects || []).map(path => `<option value="${view.h(path)}">`).join('');
        fillModels(current.id);
      };
      fillModels(initialMachine.id);
    },
    onSubmit: async form => {
      const values = Object.fromEntries(form);
      const id = values.machineId;
      if (values.ultra) values.effort = 'ultra';
      delete values.machineId;
      delete values.ultra;
      Object.keys(values).forEach(key => { if (!values[key]) delete values[key]; });
      values.saveDefaults = Object.keys(values).filter(key => ['model', 'effort'].includes(key));
      const result = await api(`/machines/${encodeURIComponent(id)}/threads`, { method: 'POST', body: values });
      closeModal();
      const projectCwd = result.thread.cwd || values.cwd;
      state.collapsedMachines.delete(id);
      await changeFilter(projectCwd ? view.projectFilter(id, projectCwd) : 'sessions');
      await selectThread(id, result.thread.id, { archived: false });
      $('message-input').focus();
    },
  });
}

function renameModal() {
  openModal({
    title: t("重命名会话"), body: `<label class="field"><span data-i18n="会话名称">${t("会话名称")}</span><input name="name" value="${view.h(view.titleOf(state.detail))}" required autocomplete="off"></label>`,
    onSubmit: async form => {
      await api(`${endpoint()}/rename`, { method: 'POST', body: { name: form.get('name') } });
      state.detail.name = form.get('name');
      closeModal();
      renderHeader();
      await refreshThreads();
    },
  });
}

function infoModal() {
  const thread = state.detail;
  const parent = thread.forkedFromId;
  openModal({
    title: t("会话信息"), footer: false,
    body: `<dl class="info-grid"><dt data-i18n="执行机器">${t("执行机器")}</dt><dd>${view.h(machine(state.selected.machineId)?.name)}</dd><dt data-i18n="工作目录">${t("工作目录")}</dt><dd>${view.h(thread.cwd)}</dd><dt data-i18n="会话 ID">${t("会话 ID")}</dt><dd><code>${view.h(thread.id)}</code></dd><dt data-i18n="状态">${t("状态")}</dt><dd>${view.h(view.statusLabel(thread))}</dd><dt data-i18n="模型">${t("模型")}</dt><dd>${view.h(state.settings.model || '—')}</dd><dt data-i18n="推理强度">${t("推理强度")}</dt><dd>${view.h(state.settings.reasoningEffort === 'ultra' ? 'Auto' : composer.effortLabel(state.settings.reasoningEffort) || '—')}</dd><dt>Ultra</dt><dd>${state.settings.reasoningEffort === 'ultra' ? t("开启") : t("已关闭")}</dd><dt data-i18n="服务档位">${t("服务档位")}</dt><dd>${view.h(state.settings.serviceTier || '—')}</dd><dt data-i18n="模式">${t("模式")}</dt><dd>${state.settings.collaborationMode?.mode === 'plan' ? t("计划") : t("默认")}</dd>${parent ? `<dt data-i18n="来源会话">${t("来源会话")}</dt><dd><code>${view.h(parent)}</code></dd>` : ''}</dl><div class="status-context">${composer.contextContents(state.tokenUsage)}</div>`,
  });
}

async function modelModal(effortOnly = false) {
  const key = selectedKey();
  const defaults = effortOnly ? { models: {} } : await api('/session-defaults');
  if (key !== selectedKey()) return;
  const model = state.settings.model;
  const effort = state.settings.reasoningEffort;
  const standard = composer.standardEffort(state.currentModels, model, effort === 'ultra' ? savedEffort(model) : effort);
  openModal({
    title: effortOnly ? t("推理强度") : t("模型与推理强度"),
    body: `${effortOnly ? '' : `<label class="field"><span data-i18n="模型">${t("模型")}</span><select name="model" id="settings-model">${composer.modelOptions(state.currentModels, model)}</select></label>`}<label class="field"><span data-i18n="推理强度">${t("推理强度")}</span><select name="effort" id="settings-effort"></select></label>${composer.ultraField('settings-ultra', effort === 'ultra')}`,
    afterRender: () => {
      const fill = options => refreshEffortField(state.currentModels, $('settings-model')?.value || model, 'settings-effort', 'settings-ultra', options);
      fill({ effort: standard });
      $('settings-ultra').onchange = () => fill();
      if ($('settings-model')) $('settings-model').onchange = event => {
        const model = event.target.value;
        const preferred = composer.supportedEffort(state.currentModels, model, defaults.models[model]?.effort);
        $('settings-ultra').checked = preferred === 'ultra';
        fill({ effort: preferred === 'ultra' ? '' : preferred || '' });
      };
    },
    onSubmit: async form => {
      const settings = {};
      if (form.get('model')) settings.model = form.get('model');
      if (form.get('ultra')) settings.effort = 'ultra';
      else if (form.get('effort')) settings.effort = form.get('effort');
      await patchSettings(settings);
      closeModal();
    },
  });
}

function permissionsModal() {
  const policy = state.settings.approvalPolicy;
  const sandbox = state.settings.sandbox;
  const sandboxMode = typeof sandbox === 'string' ? sandbox : ({ readOnly: 'read-only', workspaceWrite: 'workspace-write', dangerFullAccess: 'danger-full-access' }[sandbox?.type]);
  openModal({
    title: t("会话权限"),
    body: `<label class="field"><span data-i18n="文件访问">${t("文件访问")}</span><select name="sandbox"><option value="" data-i18n="沿用当前设置">${t("沿用当前设置")}</option><option value="read-only" ${sandboxMode === 'read-only' ? 'selected' : ''} data-i18n="只读">${t("只读")}</option><option value="workspace-write" ${sandboxMode === 'workspace-write' ? 'selected' : ''} data-i18n="工作目录可写">${t("工作目录可写")}</option><option value="danger-full-access" ${sandboxMode === 'danger-full-access' ? 'selected' : ''} data-i18n="完整访问">${t("完整访问")}</option></select></label><label class="field"><span data-i18n="执行审批">${t("执行审批")}</span><select name="approvalPolicy"><option value="" data-i18n="沿用当前设置">${t("沿用当前设置")}</option><option value="on-request" ${policy === 'on-request' ? 'selected' : ''} data-i18n="按需审批">${t("按需审批")}</option><option value="never" ${policy === 'never' ? 'selected' : ''} data-i18n="直接执行">${t("直接执行")}</option><option value="untrusted" ${policy === 'untrusted' ? 'selected' : ''} data-i18n="不信任">${t("不信任")}</option></select></label>`,
    onSubmit: async form => {
      const settings = Object.fromEntries([...form].filter(([, value]) => value));
      await patchSettings(settings);
      closeModal();
    },
  });
}

async function setPlanMode(mode) {
  await patchSettings({ collaborationMode: {
    mode,
    settings: { model: state.settings.model, reasoning_effort: state.settings.reasoningEffort || null, developer_instructions: null },
  } });
}

function reviewModal() {
  openModal({
    title: t("代码审查"), submitLabel: t("开始审查"),
    body: `<label class="field"><span data-i18n="审查目标">${t("审查目标")}</span><select name="target" id="review-target"><option value="uncommittedChanges" data-i18n="工作区改动">${t("工作区改动")}</option><option value="baseBranch" data-i18n="与分支比较">${t("与分支比较")}</option><option value="commit" data-i18n="指定提交">${t("指定提交")}</option><option value="custom" data-i18n="自定义范围">${t("自定义范围")}</option></select></label><label class="field" id="review-value-field" hidden><span id="review-value-label"></span><input name="value" id="review-value" autocomplete="off"></label>`,
    afterRender: () => {
      $('review-target').onchange = event => {
        const type = event.target.value;
        $('review-value-field').hidden = type === 'uncommittedChanges';
        $('review-value-label').textContent = { baseBranch: t("分支"), commit: t("提交 SHA"), custom: t("审查要求") }[type] || '';
      };
    },
    onSubmit: async form => {
      const type = form.get('target');
      const value = form.get('value').trim();
      const target = { type, ...(type === 'baseBranch' ? { branch: value } : type === 'commit' ? { sha: value } : type === 'custom' ? { instructions: value } : {}) };
      await api(`${endpoint()}/review`, { method: 'POST', body: { target, delivery: 'inline' } });
      closeModal();
      conversation.scheduleRefresh();
    },
  });
}

async function diffModal() {
  const result = await api(`${endpoint()}/diff`);
  openModal({
    title: t("工作区改动"), footer: false,
    body: result.diff ? `<pre class="workspace-diff">${result.diff.split('\n').map(line => `<span class="diff-line ${line.startsWith('+') && !line.startsWith('+++') ? 'added' : line.startsWith('-') && !line.startsWith('---') ? 'removed' : ''}">${view.h(line)}</span>`).join('')}</pre>` : `<div class="empty-inline">${view.h(result.stderr || t("暂无改动"))}</div>`,
  });
}

async function skillsModal() {
  const result = await api(`${endpoint()}/skills`);
  const skills = (result.data || []).flatMap(entry => entry.skills || [entry]).filter(skill => skill.enabled !== false);
  openModal({
    title: t("技能"), footer: false,
    body: skills.length ? skills.map((skill, index) => `<button type="button" class="catalog-row" data-use-skill="${index}">${icon('sparkles')}<span><strong>${view.h(skill.name)}</strong><small>${view.h(skill.description || skill.shortDescription || '')}</small></span></button>`).join('') : `<div class="empty-inline" data-i18n="暂无可用技能">${t("暂无可用技能")}</div>`,
    afterRender: () => {
      $('modal-body').querySelectorAll('[data-use-skill]').forEach(button => {
        button.onclick = () => {
          const skill = skills[Number(button.dataset.useSkill)];
          const selected = state.skillInputs.get(selectedKey()) || [];
          if (!selected.some(item => item.path === skill.path)) selected.push({ name: skill.name, path: skill.path });
          state.skillInputs.set(selectedKey(), selected);
          $('message-input').value = `$${skill.name} ${$('message-input').value}`;
          saveDraft();
          closeModal();
          resizeComposer();
          $('message-input').focus();
        };
      });
    },
  });
}

async function mcpModal() {
  const result = await api(`${endpoint()}/mcp`);
  openModal({
    title: t("MCP 工具"), footer: false,
    body: result.data?.length ? result.data.map(server => {
      const tools = Array.isArray(server.tools) ? server.tools : Object.values(server.tools || {});
      return `<details class="mcp-server"><summary>${icon('server')}<strong>${view.h(server.name)}</strong><span>${t("{0} 个工具", tools.length)}</span></summary>${tools.map(tool => `<div class="mcp-tool"><strong>${view.h(tool.name)}</strong><p>${view.h(tool.description || '')}</p></div>`).join('')}</details>`;
    }).join('') : `<div class="empty-inline" data-i18n="暂无 MCP 服务器">${t("暂无 MCP 服务器")}</div>`,
  });
}

function closeSlashMenu() {
  $('slash-menu').hidden = true;
  $('message-input').removeAttribute('aria-activedescendant');
  $('message-input').setAttribute('aria-expanded', 'false');
}

function updateSlashMenu() {
  const input = $('message-input');
  const match = input.value.match(/^\/([a-z-]*)$/i);
  if (!match || state.slashDismissed || input.selectionStart !== input.value.length) { closeSlashMenu(); return; }
  state.slashCommands = composer.slashCommands().filter(command => command.name.startsWith(match[1].toLowerCase()) && (command.name !== 'fast' || composer.speedTiers(state.currentModels, state.settings.model).length > 1));
  if (!state.slashCommands.length) { closeSlashMenu(); return; }
  state.slashIndex = Math.min(state.slashIndex, state.slashCommands.length - 1);
  renderSlashMenu();
}

function renderSlashMenu() {
  $('slash-menu').hidden = false;
  $('message-input').setAttribute('aria-expanded', 'true');
  $('slash-menu').innerHTML = state.slashCommands.map((command, index) => `<button type="button" role="option" id="slash-command-${index}" class="slash-option ${index === state.slashIndex ? 'selected' : ''}" aria-selected="${index === state.slashIndex}" data-slash-index="${index}">${icon(command.icon)}<span class="slash-command">/${command.name}</span><span class="slash-label">${view.h(command.label)}</span>${index === state.slashIndex ? `<kbd>${icon('corner-down-left')}</kbd>` : ''}</button>`).join('');
  $('message-input').setAttribute('aria-activedescendant', `slash-command-${state.slashIndex}`);
  $(`slash-command-${state.slashIndex}`)?.scrollIntoView({ block: 'nearest' });
}

async function executeSlash(name, args = '') {
  closeSlashMenu();
  if (name === 'side' || name === 'btw') {
    if (!state.selected || state.sending || state.uploadingFiles) return;
    const key = selectedKey();
    const draft = $('message-input').value;
    const attachments = [...(state.attachments.get(key) || []), ...(state.skillInputs.get(key) || []).map(skill => ({ name: skill.name, input: { type: 'skill', name: skill.name, path: skill.path } }))];
    state.sending = true;
    refreshComposer();
    try {
      const opened = await sidePanel.open({ fresh: Boolean(args.trim() || attachments.length), text: args.trim(), attachments });
      if (!opened) return;
      state.attachments.delete(key);
      state.skillInputs.delete(key);
      if (localStorage.getItem(draftStorageKey(key)) === draft) localStorage.removeItem(draftStorageKey(key));
      if (key === selectedKey() && $('message-input').value === draft) { $('message-input').value = ''; saveDraft(); resizeComposer(); }
    } finally { state.sending = false; refreshComposer(); }
    return;
  }
  $('message-input').value = '';
  saveDraft();
  resizeComposer();
  if (name === 'model') return args ? patchSettings({ model: args.trim() }) : modelModal();
  if (name === 'effort') return args ? patchSettings({ effort: args.trim() }) : modelModal(true);
  if (name === 'fast') return toggleFast();
  if (name === 'ultra') return toggleUltra();
  if (name === 'permissions') return permissionsModal();
  if (name === 'status') { await refreshSessionState(); return infoModal(); }
  if (name === 'goal') return goalPanel.command(args);
  if (name === 'new') return newThreadModal();
  if (name === 'fork') return forkThread();
  if (name === 'rename') {
    if (!args) return renameModal();
    await api(`${endpoint()}/rename`, { method: 'POST', body: { name: args.trim() } });
    state.detail.name = args.trim();
    renderHeader();
    return refreshThreads();
  }
  if (name === 'archive') return threadAction('archive');
  if (name === 'compact') {
    await api(`${endpoint()}/compact`, { method: 'POST', body: {} });
    return conversation.scheduleRefresh();
  }
  if (name === 'review') return reviewModal();
  if (name === 'plan') {
    await setPlanMode(state.settings.collaborationMode?.mode === 'plan' ? 'default' : 'plan');
    if (args) { $('message-input').value = args; saveDraft(); await sendMessage(); }
    return;
  }
  if (name === 'diff') return diffModal();
  if (name === 'ps') { state.tasksOpen = true; renderTasks(); return refreshTasks(); }
  if (name === 'skills') return skillsModal();
  if (name === 'mcp') return mcpModal();
}

function addFiles(files, kind = 'file') { return actions.addFiles(files, kind); }

async function refreshNotificationSettings() {
  const status = await notificationStatus();
  const button = $('toggle-notifications');
  if (!button) return;
  button.innerHTML = `${icon('bell')}${view.h(status.label)}`;
  button.disabled = status.disabled;
  $('test-notification').hidden = !status.enabled;
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('nexus-theme', theme);
}

function answerQuestions(event) {
  event.preventDefault();
  return actions.answer(event.target);
}

function answerRequest(event) { return actions.respond(event.target.closest('button')); }

function handleEvent(event) {
  groupChat.handleEvent(event);
  accountLimits.handleEvent(event);
  terminalBrowser.handleEvent(event);
  sidePanel.handleEvent(event);
  developerPanel.handleEvent(event);
  contextPanel.handleEvent(event);
  conversation.handleEvent(event);
  const p = event.params || {};
  const method = event.method;
  if (method === 'nexus/features/updated') {
    state.config.features = p.features;
    const control = $('feature-tool-call-id');
    if (control && !control.disabled) control.checked = p.features.toolCallId;
  }
  const restarted = state.eventEpoch && event.epoch !== state.eventEpoch;
  state.eventEpoch = event.epoch;
  if (method === 'nexus/resync' || restarted) {
    if (groupChat.visible) groupChat.refresh().catch(error => groupChat.listError(error.message));
    refreshAll();
    return;
  }
  if (method === 'nexus/search/updated') { if (state.search.trim()) scheduleListRefresh(); return; }
  if (method === 'nexus/threads/changed') {
    for (const id of p.removedThreadIds || []) applyDeleted(event.machineId, id);
    updateSummaries(event.machineId, p.updated || []);
    return;
  }
  if (method === 'nexus/machine/updated') {
    const previousStatus = machine(p.machine?.id)?.status;
    if (p.machine?.status === 'connected' && previousStatus !== 'connected') scheduleListRefresh();
    refreshMachines().then(() => {
      if (p.machine?.id === state.selected?.machineId) {
        renderHeader();
        if (p.machine.status === 'connected' && previousStatus !== 'connected') conversation.refresh({ reload: true }).catch(error => showThreadError(error.message));
        if (p.machine.status === 'connected' && previousStatus !== 'connected') sidePanel.run(() => sidePanel.refresh({ reload: true }))();
      }
    }).catch(() => {});
    return;
  }
  if (method === 'nexus/request/created' || method === 'nexus/request/resolved' || method === 'serverRequest/resolved') { refreshRequests().catch(() => {}); return; }
  if (method === 'thread/deleted') {
    applyDeleted(event.machineId, p.threadId);
    scheduleListRefresh();
    return;
  }
  if (method === 'thread/started' && p.thread) {
    updateSummaries(event.machineId, [p.thread]);
  }
  if (method === 'thread/name/updated') {
    const thread = state.threads.find(thread => thread.id === p.threadId && thread.machineId === event.machineId);
    if (thread) updateThread({ ...thread, name: p.threadName || p.name });
    renderList();
  }
  if (method === 'thread/archived' || method === 'thread/unarchived') applyArchived(event.machineId, p.threadId, method === 'thread/archived');
  if (method === 'thread/status/changed') {
    const thread = state.threads.find(thread => thread.id === p.threadId && thread.machineId === event.machineId);
    if (thread) updateSummaries(event.machineId, [{ ...thread, status: p.status }]);
  }
}

function handleThreadEvent(event, turnChanged) {
  const p = event.params || {};
  const method = event.method;
  if (method === 'thread/started') {
    applySessionState({ thread: p.thread });
  } else if (method === 'thread/settings/updated') {
    applySessionState({ settings: p.threadSettings || p.settings || p });
    scheduleConversation();
  } else if (method === 'thread/tokenUsage/updated') {
    state.tokenUsage = p.tokenUsage;
    renderSessionControls();
  } else if (method === 'thread/goal/updated' || method === 'thread/goal/cleared') {
    refreshComposer();
  } else if (method === 'thread/status/changed') {
    state.detail.status = p.status;
    updateThread(state.detail);
    renderList();
    renderHeader();
    refreshComposer();
  } else if (turnChanged) {
    conversationRenderer.invalidate(p.turnId || p.turn?.id, p.itemId || p.item?.id);
    if (method === 'turn/started' || method === 'turn/completed') {
      state.detail.status = { type: method === 'turn/started' ? 'active' : 'idle' };
    }
    if (method === 'turn/completed') {
      refreshRequests().catch(() => {});
      if (state.tasksOpen) refreshTasks().catch(() => {});
    }
    if (state.tasksOpen && method === 'item/completed' && p.item.type === 'commandExecution') refreshTasks().catch(() => {});
    scheduleConversation();
  } else if (method === 'error') showThreadError(p.error?.message || p.message || t("执行出错"));
  else if (method === 'thread/archived' || method === 'thread/unarchived') applyArchived(event.machineId, p.threadId, method === 'thread/archived');
  else if (method === 'thread/name/updated') { state.detail.name = p.threadName || p.name; renderHeader(); }
  else if (method === 'thread/reverted') state.tokenUsage = null;
}

async function changeFilter(filter) {
  if (filter === 'groups') return openGroups();
  if (filter === 'files') return openFiles();
  if (filter === 'terminals') return openTerminals();
  showSessions();
  state.filter = filter;
  state.list = { sources: [], loading: false, loadingMore: false };
  state.recentLimit = 10;
  state.sessionGroups = { unarchived: true, archived: Boolean(state.search.trim()) };
  state.errors = [];
  $('session-list').scrollTop = 0;
  hideNavigation();
  renderList();
  await refreshThreads();
}

function applyDeleted(machineId, threadId) {
  const key = `${machineId}/${threadId}`;
  const keep = thread => view.threadKey(thread) !== key;
  state.threads = state.threads.filter(keep);
  state.liveThreads = state.liveThreads.filter(keep);
  // Invalidate pending list reads while retaining the loaded pages.
  state.list = {
    ...state.list,
    sources: state.list.sources.map(source => ({ ...source, data: source.data.filter(keep) })),
    loading: false, loadingMore: false,
  };
  if (key === selectedKey()) {
    saveDraft();
    localStorage.removeItem('nexus-selected');
    history.replaceState(null, '', location.pathname + location.search);
    clearSelection();
  }
  renderList();
}

function applyArchived(machineId, threadId, archived) {
  const key = `${machineId}/${threadId}`;
  const thread = state.threads.find(thread => view.threadKey(thread) === key);
  if (thread) updateThread({ ...thread, archived });
  if (archived) state.liveThreads = state.liveThreads.filter(thread => view.threadKey(thread) !== key);
  for (const source of state.list.sources) {
    source.data = source.data.filter(thread => view.threadKey(thread) !== key);
    if (thread && source.machineId === machineId && source.archived === archived) source.data.push({ ...thread, archived });
  }
  if (key === selectedKey()) {
    state.selected.archived = archived;
    if (state.detail) state.detail.archived = archived;
    localStorage.setItem('nexus-selected', JSON.stringify(state.selected));
    renderHeader();
    refreshComposer();
  }
  renderList();
}

async function setThreadArchived(machineId, threadId, archived) {
  const key = `${machineId}/${threadId}`;
  const result = await api(`${threadPath(machineId, threadId)}/${archived ? 'archive' : 'unarchive'}`, {
    method: 'POST', body: archived ? { discardEmpty: !hasThreadDraft(key) } : {},
  });
  if (result.deleted) applyDeleted(machineId, threadId);
  else applyArchived(machineId, threadId, archived);
  await refreshThreads();
  toast(result.deleted ? t("空会话已删除") : archived ? t("会话已归档") : t("已取消归档"));
}

async function threadAction(action, button) {
  $('thread-menu').open = false;
  if (action === 'tasks') {
    state.tasksOpen = !state.tasksOpen;
    renderTasks();
    if (state.tasksOpen) await refreshTasks();
    return;
  }
  if (action === 'terminal') {
    if (!state.selected || !state.detail?.cwd) return;
    const { machineId, threadId } = state.selected;
    button.disabled = true;
    try { await terminalBrowser.create(machineId, state.detail.cwd, threadId); }
    finally { button.disabled = false; }
    return;
  }
  if (action === 'groups') return groupChat.manageThread({ ...state.detail, ...state.selected });
  if (action === 'rename') return renameModal();
  if (action === 'copy-id' || action === 'copy-short-id') {
    const id = state.selected.threadId;
    await copyText(action === 'copy-short-id' ? id.slice(-6).toLowerCase() : id);
    toast(t("会话 ID 已复制"));
    return;
  }
  if (action === 'developer-inputs') { developerPanel.show(!developerPanel.opened); return; }
  if (action === 'context-usage') { contextPanel.show(!contextPanel.opened); return; }
  if (action === 'new') return createProjectThread({ machineId: state.selected.machineId, cwd: state.detail.cwd }, $('new-thread-action'));
  if (action === 'archive') {
    await setThreadArchived(state.selected.machineId, state.selected.threadId, !state.selected.archived);
  }
}

function changeSearch() {
  const previous = Boolean(state.search.trim());
  state.search = $('session-search').value;
  state.searchMode = $('search-mode').value;
  $('clear-session-search').hidden = !state.search;
  state.list = { sources: [], loading: false, loadingMore: false, searchPending: Boolean(state.search.trim()) };
  if (previous !== Boolean(state.search.trim())) state.sessionGroups = { unarchived: true, archived: Boolean(state.search.trim()) };
  state.recentLimit = 10;
  renderList();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => refreshThreads().catch(error => toast(error.message, true)), 250);
}
$('session-search').addEventListener('input', changeSearch);
$('search-mode').addEventListener('change', changeSearch);
$('clear-session-search').addEventListener('click', () => {
  const input = $('session-search');
  input.value = '';
  input.dispatchEvent(new Event('input'));
  input.focus();
});
document.querySelector('.sidebar').addEventListener('contextmenu', event => {
  const project = event.target.closest('.nav-tree-row')?.querySelector('.project-row');
  if (!project) return;
  event.preventDefault();
  const { machineId, cwd } = view.projectFromFilter(project.dataset.filter);
  showContextMenu({ target: event.target, x: event.clientX, y: event.clientY, className: 'project-context-menu' }, [
    ['terminal', t("打开终端"), run(() => terminalBrowser.create(machineId, cwd))],
    ['copy', t("复制项目名称"), run(async () => {
      await copyText(view.basename(cwd));
      toast(t("项目名称已复制"));
    })],
    ['copy', t("复制项目路径"), run(async () => {
      await copyText(cwd);
      toast(t("项目路径已复制"));
    })],
  ]);
});
document.querySelector('.sidebar').addEventListener('click', run(async event => {
  const toggle = event.target.closest('[data-toggle-machine]');
  if (toggle) {
    const id = toggle.dataset.toggleMachine;
    if (state.collapsedMachines.has(id)) state.collapsedMachines.delete(id);
    else state.collapsedMachines.add(id);
    localStorage.setItem('nexus-collapsed-machines', JSON.stringify([...state.collapsedMachines]));
    renderNavigation();
    return;
  }
  const addProject = event.target.closest('[data-add-project]');
  if (addProject) { addProjectModal(addProject.dataset.addProject); return; }
  const remove = event.target.closest('[data-remove-project]');
  if (remove) { await removeProject(remove); return; }
  const newThread = event.target.closest('[data-project-new-thread]');
  if (newThread) {
    const filter = newThread.dataset.projectNewThread;
    await createProjectThread(view.projectFromFilter(filter), newThread);
    await changeFilter(filter);
    return;
  }
  const filter = event.target.closest('[data-filter]')?.dataset.filter;
  if (filter) await changeFilter(filter);
}));
$('session-list').addEventListener('click', run(async event => {
  const group = event.target.closest('[data-session-group]');
  if (group) { state.sessionGroups[group.dataset.sessionGroup] = !state.sessionGroups[group.dataset.sessionGroup]; renderList(); return; }
  const archive = event.target.closest('[data-archive-thread]');
  if (archive) { await setThreadArchived(archive.dataset.machineId, archive.dataset.archiveThread, archive.dataset.archived !== 'true'); return; }
  const more = event.target.closest('[data-load-more]');
  if (more) { await loadMoreThreads(more.dataset.loadMore); return; }
  const button = event.target.closest('[data-thread-id]');
  if (button) await selectThread(button.dataset.machineId, button.dataset.threadId, { archived: button.dataset.archived === 'true' });
}));
const scrollRecent = () => {
  const list = $('session-list');
  if (!currentProject() && list.scrollHeight - list.scrollTop - list.clientHeight < 100) run(loadMoreThreads)();
};
$('session-list').addEventListener('scroll', () => { if ($('session-list').scrollTop > 0) scrollRecent(); });
$('session-list').addEventListener('wheel', event => { if (event.deltaY > 0) scrollRecent(); }, { passive: true });
for (const id of ['new-thread', 'welcome-new']) $(id).addEventListener('click', run(newThreadModal));
$('project-new-thread').addEventListener('click', run(newProjectThread));
$('welcome-machines').addEventListener('click', () => machineForm());
$('machines').addEventListener('click', () => machinesModal());
$('account-usage').addEventListener('click', () => { hideNavigation(); accountLimits.open(); });
$('settings').addEventListener('click', () => settingsModal());
$('refresh-list').addEventListener('click', run(async () => {
  if (state.search.trim()) await Promise.all((currentProject() ? [machine(currentProject().machineId)] : machineFilter.selectedMachines()).map(machine => api(`/search/sync${query({ machineId: machine.id })}`, { method: 'POST' })));
  await refreshAll();
}));
$('older-history').addEventListener('click', run(loadOlderHistory));
$('toggle-side').addEventListener('click', run(() => sidePanel.opened ? sidePanel.show(false) : sidePanel.open()));
$('message-form').addEventListener('submit', run(sendMessage));
$('message-input').addEventListener('input', () => { saveDraft(); resizeComposer(); });
$('message-input').addEventListener('keydown', event => {
  if (event.isComposing || event.keyCode === 229) return;
  if (!$('slash-menu').hidden) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      state.slashIndex = (state.slashIndex + (event.key === 'ArrowDown' ? 1 : -1) + state.slashCommands.length) % state.slashCommands.length;
      renderSlashMenu();
      return;
    }
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); state.slashDismissed = true; closeSlashMenu(); return; }
    if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
      event.preventDefault();
      run(executeSlash)(state.slashCommands[state.slashIndex].name);
      return;
    }
  }
});
$('message-input').addEventListener('input', () => { state.slashDismissed = false; state.slashIndex = 0; updateSlashMenu(); });
$('message-input').addEventListener('click', updateSlashMenu);
$('slash-menu').addEventListener('mousedown', event => event.preventDefault());
$('slash-menu').addEventListener('click', run(async event => {
  const option = event.target.closest('[data-slash-index]');
  if (option) await executeSlash(state.slashCommands[Number(option.dataset.slashIndex)].name);
}));
$('composer-model').addEventListener('change', run(setModel));
$('composer-effort').addEventListener('change', run(setEffort));
$('composer-ultra').addEventListener('click', run(toggleUltra));
$('composer-fast').addEventListener('click', run(toggleFast));
$('composer-tool-compact').addEventListener('click', run(() => patchSettings({ toolCallCompact: !state.settings.toolCallCompact })));
for (const id of ['interrupt-button', 'working-interrupt']) $(id).addEventListener('click', run(interruptTurn));
for (const id of ['resume-button', 'working-resume']) $(id).addEventListener('click', run(resumeTurn));
$('close-tasks').addEventListener('click', () => { state.tasksOpen = false; renderTasks(); });
$('refresh-tasks').addEventListener('click', run(refreshTasks));
$('clean-tasks').addEventListener('click', run(cleanTasks));
$('task-list').addEventListener('click', run(async event => { const id = event.target.closest('[data-stop-task]')?.dataset.stopTask; if (id) await stopTask(id); }));
$('conversation').addEventListener('click', run(async event => {
  const menuAction = event.target.closest('.turn-menu button');
  if (menuAction) menuAction.closest('.turn-menu').open = false;
  const image = event.target.closest('[data-preview-image]')?.querySelector('img');
  if (image) { openModal({ title: t("图片"), body: `<img class="image-preview" src="${view.h(image.src)}" alt="${t("用户图片")}" data-i18n-alt="用户图片">`, footer: false }); return; }
  const edit = event.target.closest('[data-edit-message]');
  if (edit) return editMessage(edit.dataset.editMessage, edit.closest('[data-turn-id]').dataset.turnId);
  if (event.target.closest('[data-cancel-edit]')) return cancelEdit();
  const agent = event.target.closest('[data-open-agent]');
  if (agent) return selectThread(state.selected.machineId, agent.dataset.openAgent);
  const forkMessage = event.target.closest('[data-fork-message]');
  if (forkMessage) return forkThread(forkMessage.closest('[data-turn-id]').dataset.turnId, forkMessage.dataset.forkMessage);
  const regenerateMessageButton = event.target.closest('[data-regenerate-message]');
  if (regenerateMessageButton) return regenerateMessage(regenerateMessageButton.dataset.regenerateMessage, regenerateMessageButton.closest('[data-turn-id]').dataset.turnId);
  const fork = event.target.closest('[data-fork-turn]');
  if (fork) return forkThread(fork.dataset.forkTurn);
  const regenerate = event.target.closest('[data-regenerate-turn]');
  if (regenerate) return regenerateTurn(regenerate.dataset.regenerateTurn);
  const copyMessage = event.target.closest('[data-copy-message]');
  if (copyMessage) {
    const turn = conversation.visibleTurns.find(turn => turn.id === copyMessage.closest('[data-turn-id]').dataset.turnId);
    const item = turn.items.find(item => item.id === copyMessage.dataset.copyMessage);
    await navigator.clipboard.writeText(view.itemText(item));
    return toast(t("消息已复制"));
  }
  const copy = event.target.closest('[data-copy-turn]');
  if (copy) {
    const turn = conversation.turns.find(turn => turn.id === copy.dataset.copyTurn);
    await navigator.clipboard.writeText(turn.items.filter(item => item.type === 'agentMessage').map(view.itemText).join('\n\n'));
    toast(t("本轮回复已复制"));
  }
}));
$('conversation').addEventListener('submit', run(event => event.target.matches('[data-question-form]') ? answerQuestions(event) : saveEditedMessage(event)));
$('conversation').addEventListener('nexus/visualization-follow-up', run(async event => {
  await api(`${endpoint()}/messages`, { method: 'POST', body: { text: event.detail.prompt, actor: 'human' } });
}));
$('thread-view').querySelector('.thread-header').addEventListener('click', run(async event => {
  const button = event.target.closest('[data-thread-action]');
  if (button) await threadAction(button.dataset.threadAction, button);
}));
$('pending-requests').addEventListener('click', run(answerRequest));
$('pending-requests').addEventListener('submit', run(answerQuestions));
for (const container of [$('conversation'), $('pending-requests')]) {
  bindQuestionInputs(container, () => { scheduleConversation(); renderRequests(); });
}
$('permission-info').addEventListener('click', permissionsModal);
$('attachment-menu').addEventListener('toggle', event => {
  if (event.newState !== 'open') return;
  const menu = $('attachment-menu');
  const anchor = $('attach-file').getBoundingClientRect();
  menu.style.left = `${Math.min(anchor.left, innerWidth - menu.offsetWidth - 12)}px`;
  menu.style.top = `${anchor.top - menu.offsetHeight - 8}px`;
});
$('attachment-menu').addEventListener('click', event => {
  const choice = event.target.closest('[data-upload-kind]');
  if (!choice) return;
  $('attachment-menu').hidePopover();
  $(choice.dataset.uploadKind === 'image' ? 'attachment-images' : 'attachment-files').click();
});
$('attachment-images').addEventListener('change', run(async event => {
  const files = [...event.target.files];
  event.target.value = '';
  await addFiles(files, 'image');
}));
$('attachment-files').addEventListener('change', run(async event => {
  const files = [...event.target.files];
  event.target.value = '';
  await addFiles(files);
}));
$('message-input').addEventListener('paste', run(async event => {
  const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === 'file').map(item => item.getAsFile());
  if (files.length) { event.preventDefault(); await addFiles(files, files.every(file => file.type.startsWith('image/')) ? 'image' : 'file'); }
}));
const composerArea = document.querySelector('.composer-area');
composerArea.addEventListener('dragover', event => {
  if ([...event.dataTransfer.items].some(item => item.kind === 'file')) {
    event.preventDefault();
    composerArea.classList.add('drag-over');
  }
});
composerArea.addEventListener('dragleave', event => {
  if (!composerArea.contains(event.relatedTarget)) composerArea.classList.remove('drag-over');
});
composerArea.addEventListener('drop', run(async event => {
  event.preventDefault();
  composerArea.classList.remove('drag-over');
  const files = [...event.dataTransfer.files];
  await addFiles(files, event.shiftKey && files.every(file => file.type.startsWith('image/')) ? 'image' : 'file');
}));
$('attachment-chips').addEventListener('click', event => {
  const preview = event.target.closest('[data-preview-attachment]');
  if (preview) {
    const attachment = state.attachments.get(selectedKey())[Number(preview.dataset.previewAttachment)];
    openModal({ title: attachment.name, body: `<img class="image-preview" src="${view.h(attachment.preview)}" alt="${view.h(attachment.name)}">`, footer: false });
  }
  const button = event.target.closest('[data-remove-attachment]');
  if (button) { state.attachments.get(selectedKey()).splice(Number(button.dataset.removeAttachment), 1); refreshComposer(); }
  const skill = event.target.closest('[data-remove-skill]');
  if (skill) { state.skillInputs.get(selectedKey()).splice(Number(skill.dataset.removeSkill), 1); refreshComposer(); }
});
$('modal-close').addEventListener('click', closeModal);
$('modal-cancel').addEventListener('click', closeModal);
$('modal-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = $('modal-submit');
  if (!state.modalHandler || submit.disabled) return;
  submit.disabled = true;
  submit.setAttribute('aria-busy', 'true');
  try {
    const complete = await state.modalHandler(new FormData(event.target));
    if (complete !== false) $('modal-error').hidden = true;
  }
  catch (error) { $('modal-error').textContent = error.message; $('modal-error').hidden = false; }
  finally { submit.disabled = false; submit.removeAttribute('aria-busy'); }
});
$('modal-body').addEventListener('click', run(async event => {
  if (event.target.closest('#toggle-notifications')) {
    const button = $('toggle-notifications');
    button.disabled = true;
    try { toast(await toggleNotifications() ? t("完成通知已开启") : t("完成通知已关闭")); }
    finally { await refreshNotificationSettings(); }
    return;
  }
  if (event.target.closest('#test-notification')) {
    const button = $('test-notification');
    button.disabled = true;
    button.textContent = notificationMessages().pending;
    try {
      await testNotification();
      toast(notificationMessages().received);
    } finally {
      button.disabled = false;
      button.textContent = t("测试通知");
      await refreshNotificationSettings();
    }
    return;
  }
  if (event.target.closest('[data-install-app]')) return installApp(openModal);
  const tab = event.target.closest('[data-settings-tab]');
  if (tab) return settingsModal(tab.dataset.settingsTab);
  if (event.target.closest('[data-add-machine]')) return machineForm();
  const edit = event.target.closest('[data-machine-edit]');
  if (edit) return machineForm(machine(edit.dataset.machineEdit));
  const find = event.target.closest('[data-machine-find]');
  if (find) return findMachineThreads(machine(find.dataset.machineFind));
  const connection = event.target.closest('[data-machine-action]');
  if (connection) {
    const { machineId, machineAction } = connection.dataset;
    if (state.machineActions.has(machineId)) return;
    state.machineActions.add(machineId);
    connection.closest('.machine-card-actions').querySelectorAll('[data-machine-action]').forEach(button => { button.disabled = true; });
    try { await api(`/machines/${encodeURIComponent(machineId)}/${machineAction}`, { method: 'POST', body: {}, timeout: 60_000 }); }
    finally {
      state.machineActions.delete(machineId);
      await refreshAll();
    }
    return;
  }
  const remove = event.target.closest('[data-machine-delete]');
  if (remove) {
    await api(`/machines/${encodeURIComponent(remove.dataset.machineDelete)}`, { method: 'DELETE' });
    if (state.selected?.machineId === remove.dataset.machineDelete) clearSelection();
    await refreshAll();
    toast(t("机器配置已移除"));
  }
}));
$('theme-toggle').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme;
  setTheme(theme === 'dark' ? 'light' : theme === 'light' ? 'system' : 'dark');
  toast({ light: t("浅色外观"), dark: t("深色外观"), system: t("跟随系统外观") }[document.documentElement.dataset.theme]);
});
$('open-nav').addEventListener('click', showNavigation);
$('close-nav').addEventListener('click', hideNavigation);
$('nav-scrim').addEventListener('click', hideNavigation);
$('back-to-list').addEventListener('click', () => $('app').classList.remove('mobile-detail'));
document.querySelector('.brand').addEventListener('click', event => { event.preventDefault(); showSessions({ updateHash: false }); clearSelection(); history.replaceState(null, '', location.pathname); });
document.addEventListener('click', event => {
  if (!event.target.closest('#thread-menu')) $('thread-menu').open = false;
  document.querySelectorAll('.context-usage[open]').forEach(usage => { if (!usage.contains(event.target)) usage.open = false; });
  if (!event.target.closest('#slash-menu') && event.target.id !== 'message-input') closeSlashMenu();
});
document.addEventListener('keydown', composer.submitOnEnter);
document.addEventListener('keydown', event => {
  if ($('offline-screen').open) return;
  if (state.section === 'terminals' && event.target.closest('.xterm')) return;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); run(newThreadModal)(); }
  if (event.key === 'Escape') { hideNavigation(); $('thread-menu').open = false; }
});
window.addEventListener('hashchange', run(async () => {
  if (location.hash.startsWith('#groups')) return openGroups(decodeURIComponent(location.hash.slice('#groups/'.length) || '') || undefined);
  if (location.hash.startsWith('#files')) return openFiles(fileRoute());
  if (location.hash.startsWith('#terminals')) return openTerminals(decodeURIComponent(location.hash.split('/')[1] || '') || undefined);
  showSessions({ updateHash: false });
  const [machineId, threadId] = location.hash.slice(1).split('/').map(decodeURIComponent);
  if (machineId && threadId && `${machineId}/${threadId}` !== selectedKey()) await selectThread(machineId, threadId, { updateHash: false });
}));
navigator.serviceWorker?.addEventListener('message', event => {
  if (event.data?.type === 'nexus/open-thread') location.hash = new URL(event.data.url).hash;
});
$('offline-screen').addEventListener('cancel', event => event.preventDefault());
$('offline-reconnect').addEventListener('click', reconnectClient);
window.addEventListener('offline', () => setConnection(false));
window.addEventListener('online', reconnectClient);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && events) {
    if (!state.online) reconnectClient();
    else syncClient().catch(error => showThreadError(error.message));
  }
});
window.addEventListener('beforeunload', () => { sidePanel.saveDraft(); events?.close(); clearInterval(turnTimer); });

function setConnection(online) {
  if (state.online === online) return;
  state.online = online;
  const screen = $('offline-screen');
  if (online) screen.close();
  else {
    $('toast-region').hidePopover();
    screen.showModal();
  }
  renderNavigation();
  renderHeader();
}

function reconnectClient() {
  if (events) {
    events.close();
    connectEvents();
  } else if ($('offline-screen').open) location.reload();
}

async function syncClient() {
  await refreshAll();
  accountLimits.refresh();
  if (groupChat.visible) await groupChat.refresh();
  if (state.selected && machine(state.selected.machineId)?.status === 'connected') {
    await conversation.refresh({ reload: true });
    await sidePanel.refresh({ reload: true });
    await developerPanel.refresh();
    await contextPanel.refresh();
    showThreadError();
  }
}

function connectEvents() {
  events = openEvents(handleEvent, online => {
    const reconnecting = !state.online;
    setConnection(online);
    if (online && reconnecting) syncClient().catch(error => showThreadError(error.message));
  });
}

async function boot() {
  state.config = await api('/config');
  $('demo-label').hidden = !state.config.demo;
  connectEvents();
  restoreNotifications().catch(error => console.error('Nexus notifications:', error));
  await refreshAll();
  if (location.hash.startsWith('#groups')) {
    await openGroups(decodeURIComponent(location.hash.slice('#groups/'.length) || '') || undefined);
    turnTimer = setInterval(() => { refreshTurnTimer(); sidePanel.tick(); }, 1000);
    return;
  }
  if (location.hash.startsWith('#terminals')) {
    await openTerminals(decodeURIComponent(location.hash.split('/')[1] || '') || undefined);
    turnTimer = setInterval(() => { refreshTurnTimer(); sidePanel.tick(); }, 1000);
    return;
  }
  if (location.hash.startsWith('#files')) {
    await openFiles(fileRoute());
    turnTimer = setInterval(() => { refreshTurnTimer(); sidePanel.tick(); }, 1000);
    return;
  }
  const [machineId, threadId] = location.hash.slice(1).split('/').map(decodeURIComponent);
  let previous = null;
  try { previous = JSON.parse(localStorage.getItem('nexus-selected')); } catch { /* No saved selection. */ }
  const selection = machineId && threadId ? { machineId, threadId } : previous;
  let restore = false;
  if (selection && machine(selection.machineId)) {
    const cached = state.threads.find(thread => thread.machineId === selection.machineId && thread.id === selection.threadId);
    const thread = cached || (await api(threadPath(selection.machineId, selection.threadId)).catch(() => null))?.thread;
    restore = thread && trackedThread({ ...thread, machineId: selection.machineId });
  }
  if (restore) await selectThread(selection.machineId, selection.threadId, { archived: selection.archived || false });
  else {
    if (selection) {
      localStorage.removeItem('nexus-selected');
      history.replaceState(null, '', location.pathname);
    }
    const first = currentProject() ? filteredThreads()[0] : sessionSections().flatMap(group => group.threads)[0];
    if (first && window.innerWidth > 720) await selectThread(first.machineId, first.id);
    else clearSelection();
  }
  turnTimer = setInterval(() => { refreshTurnTimer(); sidePanel.tick(); }, 1000);
}

if (!navigator.onLine) setConnection(false);
boot().then(() => {
  state.initializing = false;
  renderList();
}).catch(error => {
  if (!state.config.app) setConnection(false);
  $('session-loading').hidden = true;
  $('session-list').innerHTML = `<div class="list-empty">${icon('circle-alert')}<div>${view.h(error.message)}</div><button class="button subtle" onclick="location.reload()" data-i18n="重新连接">${t("重新连接")}</button></div>`;
});
