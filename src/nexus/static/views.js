import { t, locale } from './i18n.js?v=145';
import { markdown } from './markdown.js?v=145';
import { icon } from './icons.js?v=145';
import * as throughput from './throughput.js?v=145';
import { fileUrl } from './files.js?v=145';

export const h = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
export { markdown };
export const basename = path => String(path || '').replace(/\/$/, '').split(/[\\/]/).pop() || path || '';
export const titleOf = thread => {
  if (thread.name || thread.title) return thread.name || thread.title;
  const preview = thread.preview?.split('\n')[0].trim() || '';
  return preview.length > 60 ? `${preview.slice(0, 59)}…` : preview || t("新会话");
};
export const threadKey = thread => `${thread.machineId}/${thread.id}`;
export const statusType = thread => typeof thread?.status === 'string' ? thread.status : thread?.status?.type || 'notLoaded';
export const isRunning = thread => statusType(thread) === 'active';
export const isWaiting = thread => Boolean(thread?.status?.activeFlags?.some(flag => ['waitingOnApproval', 'waitingOnUserInput'].includes(flag)));
export const statusLabel = thread => isWaiting(thread) ? t("等待回应") : ({ active: t("正在运行"), idle: t("就绪"), notLoaded: t("已保存"), systemError: t("执行出错") }[statusType(thread)] || statusType(thread));
export const isTurnRunning = turn => ['inProgress', 'active', 'running'].includes(turn.status);
export const isCompactionRunning = (item, turnRunning) => ['contextCompaction', 'toolCallCompaction'].includes(item.type)
  && item.completedAtMs == null && (item._active ?? turnRunning);

export function turnDuration(turn, now = Date.now()) {
  if (!isTurnRunning(turn) && turn.durationMs != null) return Math.max(0, turn.durationMs);
  if (turn.startedAt == null) return null;
  const end = isTurnRunning(turn) ? now : turn.completedAt == null ? null : turn.completedAt * 1000;
  return end == null ? null : Math.max(0, end - turn.startedAt * 1000);
}

export function formatDuration(ms) {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m ${seconds % 60}s`;
}

export function formatDateTime(value) {
  const date = new Date(value * 1000);
  const pad = number => String(number).padStart(2, '0');
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function timeAgo(value) {
  if (!value) return '';
  const date = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value);
  const elapsed = Math.max(0, Date.now() - date.getTime());
  if (elapsed < 60000) return t("刚刚");
  if (elapsed < 3600000) return t("{0} 分钟", Math.floor(elapsed / 60000));
  if (elapsed < 86400000) return t("{0} 小时", Math.floor(elapsed / 3600000));
  if (elapsed < 604800000) return t("{0} 天", Math.floor(elapsed / 86400000));
  return date.toLocaleDateString(locale(), { month: 'numeric', day: 'numeric' });
}

export const threadTime = thread => {
  const value = thread.recencyAt || thread.updatedAt || thread.createdAt || 0;
  return typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : new Date(value).getTime();
};
export const byRecent = (a, b) => threadTime(b) - threadTime(a);
export function sessionSections(threads, requests = []) {
  const groups = [
    { key: 'waiting', label: t("等待回应"), threads: [] },
    { key: 'working', label: t("正在工作"), threads: [] },
    { key: 'other', label: t("其他"), threads: [] },
  ];
  for (const thread of [...threads].sort(byRecent)) {
    const waiting = isWaiting(thread) || requests.some(request => request.machineId === thread.machineId && request.threadId === thread.id);
    groups[waiting ? 0 : isRunning(thread) ? 1 : 2].threads.push(thread);
  }
  return groups;
}
export const projectFilter = (machineId, cwd) => `project:${encodeURIComponent(machineId)}:${encodeURIComponent(cwd)}`;
export function projectFromFilter(filter) {
  if (!filter.startsWith('project:')) return null;
  const [machineId, cwd] = filter.slice(8).split(':').map(decodeURIComponent);
  return { machineId, cwd };
}

export function machineNav(machines, threads, filter, collapsed, online = true) {
  if (!machines.length) return `<div class="nav-empty" data-i18n="暂无机器">${t("暂无机器")}</div>`;
  return machines.map(machine => {
    const activity = new Map(Object.entries(machine.projectActivity || {}).map(([cwd, time]) => [cwd, threadTime({ updatedAt: time })]));
    threads.filter(thread => thread.machineId === machine.id && thread.cwd).forEach(thread => activity.set(thread.cwd, Math.max(activity.get(thread.cwd) || 0, threadTime(thread))));
    const projects = [...(machine.projects || [])].sort((a, b) => (activity.get(b) || 0) - (activity.get(a) || 0) || a.localeCompare(b));
    const expanded = !collapsed.has(machine.id);
    const connected = online && machine.status === 'connected';
    return `<div class="machine-group"><div class="nav-tree-row"><button class="nav-row machine-toggle" data-toggle-machine="${h(machine.id)}" aria-expanded="${expanded}" data-tooltip="${h(machine.error || machine.host || machine.name)}"><i class="status-dot ${connected ? 'connected' : 'disconnected'}" role="img" aria-label="${connected ? t("已连接") : t("未连接")}"></i><span>${h(machine.name)}</span>${icon(expanded ? 'chevron-down' : 'chevron-right', 'machine-chevron')}</button><button class="icon-button small nav-add" data-add-project="${h(machine.id)}" aria-label="${t("在 {0} 加入项目", h(machine.name))}" data-tooltip="${t("加入项目")}" data-i18n-data-tooltip="加入项目">${icon('plus')}</button></div><div class="machine-projects" ${expanded ? '' : 'hidden'}>${projects.map(cwd => `<div class="nav-tree-row ${filter === projectFilter(machine.id, cwd) ? 'active' : ''}"><button class="nav-row project-row" data-filter="${h(projectFilter(machine.id, cwd))}" data-tooltip="${h(cwd)}">${icon('folder')}<span>${h(basename(cwd))}</span></button><button class="icon-button project-new-thread" data-project-new-thread="${h(projectFilter(machine.id, cwd))}" aria-label="${t("在 {0} 新建会话", h(basename(cwd)))}" data-tooltip="${t("新建会话")}" data-i18n-data-tooltip="新建会话">${icon('plus')}</button><button class="icon-button project-remove" data-remove-project="${h(projectFilter(machine.id, cwd))}" aria-label="${t("将 {0} 移出追踪", h(basename(cwd)))}" data-tooltip="${t("移出追踪")}" data-i18n-data-tooltip="移出追踪">${icon('x')}</button></div>`).join('')}</div></div>`;
  }).join('');
}

export function sessionList(threads, selected, requests, { groupDays = true } = {}) {
  if (!threads.length) return `<div class="list-empty" data-i18n="暂无会话">${t("暂无会话")}</div>`;
  let previousDay = '';
  return threads.map(thread => {
    const updated = threadTime(thread);
    const dayDiff = (new Date().setHours(0, 0, 0, 0) - new Date(updated).setHours(0, 0, 0, 0)) / 86400000;
    const day = dayDiff < 1 ? t("今天") : dayDiff < 2 ? t("昨天") : dayDiff < 7 ? t("过去 7 天") : t("更早");
    const heading = !groupDays || day === previousDay ? '' : `<div class="session-day">${day}</div>`;
    previousDay = day;
    const waiting = requests.some(r => r.machineId === thread.machineId && r.threadId === thread.id) || isWaiting(thread);
    const archived = Boolean(thread.archived);
    const action = archived ? t("取消归档") : t("归档会话");
    return `${heading}<div class="session-entry"><button class="session-row ${selected && selected.machineId === thread.machineId && selected.threadId === thread.id ? 'selected' : ''}" data-thread-id="${h(thread.id)}" data-machine-id="${h(thread.machineId)}" data-archived="${archived}"><div class="session-title-row"><span class="session-title">${h(titleOf(thread))}</span>${waiting ? icon('circle-help', 'waiting-icon') : isRunning(thread) ? '<i class="running-dot"></i>' : ''}</div><div class="session-meta"><span class="session-context">${h(thread.machineName || thread.machineId)}${thread.cwd ? ` · ${h(basename(thread.cwd))}` : ''}</span><time class="session-time">${h(timeAgo(updated))}</time></div>${thread.searchMatch ? `<div class="session-match">${h(thread.searchMatch.text)}</div>` : ''}</button><button class="icon-button small session-archive" data-archive-thread="${h(thread.id)}" data-machine-id="${h(thread.machineId)}" data-archived="${archived}" aria-label="${action}" data-tooltip="${action}">${icon(archived ? 'archive-restore' : 'archive')}</button></div>`;
  }).join('');
}

export function groupedSessionList(groups, selected, requests, more, loadingMore) {
  const sections = groups.filter(group => group.threads.length || (group.key === 'other' && more));
  if (!sections.length) return `<div class="list-empty" data-i18n="暂无会话">${t("暂无会话")}</div>`;
  return sections.map(group => `<section class="session-group" data-status-group="${group.key}"><h2 class="session-status-heading">${group.label}<span>${group.threads.length}${group.key === 'other' && more ? '+' : ''}</span></h2>${sessionList(group.threads, selected, requests, { groupDays: false })}${group.key === 'other' && more ? `<div class="load-more-area"><button class="text-button" data-load-more="recent" ${loadingMore ? 'disabled' : ''}>${loadingMore ? t("正在读取会话…") : t("加载更多会话")}</button></div>` : ''}</section>`).join('');
}

export function projectSessionList(threads, selected, requests, groups, sources, loading, loadingMore) {
  return [false, true].map(archived => {
    const key = archived ? 'archived' : 'unarchived';
    const items = threads.filter(thread => Boolean(thread.archived) === archived);
    const more = sources.some(source => source.archived === archived && source.cursor);
    return `<section class="session-group"><button class="session-group-heading" data-session-group="${key}" aria-expanded="${groups[key]}">${icon(groups[key] ? 'chevron-down' : 'chevron-right')}<span>${archived ? t("已归档") : t("未归档")}</span><span class="nav-count">${items.length}${more ? '+' : ''}</span></button><div ${groups[key] ? '' : 'hidden'}>${loading && !items.length ? `<div class="list-placeholder" data-i18n="正在读取会话…">${t("正在读取会话…")}</div>` : sessionList(items, selected, requests, { groupDays: false })}${more ? `<div class="load-more-area"><button class="text-button" data-load-more="${key}" ${loadingMore ? 'disabled' : ''}>${loadingMore ? t("正在读取会话…") : t("加载更多会话")}</button></div>` : ''}</div></section>`;
  }).join('');
}

function textContent(content) {
  if (typeof content === 'string') return content;
  return (content || []).map(part => part.text || part.content || '').filter(Boolean).join('\n');
}

export function itemText(item) {
  const text = item.text || textContent(item.content);
  const prefix = messageDelivery(item)?.promptPrefix;
  return prefix && text.startsWith(prefix) ? text.slice(prefix.length) : text;
}

function reasoningText(item) {
  return [...(item.summary || []), ...(item.content || [])].map(part => typeof part === 'string' ? part : part.text || '').filter(Boolean).join('\n\n');
}

export function messageOrigin(item) {
  const actor = item.origin || item.nexusActor || item.actor || item.source;
  const name = item.actorName || actor?.name || actor?.actorName;
  const kind = typeof actor === 'string' ? actor : actor?.type || actor?.actor;
  return { kind, name };
}

export function messageDelivery(item) {
  if (item.type !== 'userMessage') return null;
  if (item.origin?.delivery) return item.origin.delivery;
  const { kind } = messageOrigin(item);
  const text = item.text || textContent(item.content);
  const group = /^(?:In group \[([\s\S]*?)\], |在群 \[([\s\S]*?)\] 中)/.exec(text);
  if (kind !== 'agent' && !(group && kind === 'human')) return null;
  const sender = /^(?:From (?:\[([0-9a-f]{6})\]|(user))|发自(?: \[([0-9a-f]{6})\]|(用户)))(?:, (?:CC|抄送) (\[[0-9a-f]{6}\](?:, \[[0-9a-f]{6}\])*))?: /.exec(text.slice(group?.[0].length || 0));
  if (!sender) return null;
  return {
    senderBotId: sender[1] || sender[3] || null,
    groupName: group?.[1] ?? group?.[2] ?? null,
    cc: [...(sender[5] || '').matchAll(/\[([0-9a-f]{6})\]/g)].map(match => match[1]),
    promptPrefix: (group?.[0] || '') + sender[0],
  };
}

export function identicon(id) {
  const seed = parseInt(id, 16) || 0;
  const color = `hsl(${((seed >>> 15) * 137) % 360} 54% 46%)`;
  let cells = '';
  for (let row = 0; row < 5; row++) for (let column = 0; column < 3; column++) {
    if (!(seed & (1 << (row * 3 + column)))) continue;
    cells += `<rect x="${column + 1}" y="${row + 1}" width="1" height="1"/>`;
    if (column < 2) cells += `<rect x="${5 - column}" y="${row + 1}" width="1" height="1"/>`;
  }
  return `<svg class="identicon" viewBox="0 0 7 7" role="img" aria-label="${t("{0} 的头像", h(id))}" shape-rendering="crispEdges"><rect width="7" height="7" fill="#f0f1f3"/><g fill="${color}">${cells}</g></svg>`;
}

function incomingHeader(item, delivery) {
  const { kind, name } = messageOrigin(item);
  const human = kind === 'human' || kind === 'user';
  const label = name || delivery?.senderBotId || (human ? t("用户") : kind === 'tool' ? t("自动化工具") : 'Agent');
  const avatar = delivery?.senderBotId ? identicon(delivery.senderBotId) : `<span class="group-user-avatar">${icon(human ? 'user' : 'terminal')}</span>`;
  const group = delivery?.groupName == null ? '' : `<span class="message-group">${icon('messages')}<span>${h(delivery.groupName)}</span></span>`;
  return { avatar: `<span class="group-message-avatar">${avatar}</span>`, meta: `<div class="incoming-message-meta"><span class="message-sender">${h(label)}</span>${group}</div>` };
}

function copiedRecipients(delivery) {
  if (!delivery?.cc?.length) return '';
  return `<div class="message-recipients" role="group" aria-label="${t("抄送")}"><span class="message-cc-label">${t("抄送")}</span>${delivery.cc.map(id => `<span class="message-recipient">${identicon(id)}<span>${h(id)}</span></span>`).join('')}</div>`;
}

function toolItem(item, title, glyph, content, { cards = false, status = '' } = {}) {
  const running = item._active ?? ['inProgress', 'running'].includes(item.status);
  const failed = ['failed', 'declined', 'cancelled'].includes(item.status);
  const label = failed ? t("未完成") : running ? '' : item.status === 'completed' ? t("已完成") : '';
  const indicator = label ? `<span class="tool-state tool-state-dot" role="img" aria-label="${label}" data-tooltip="${label}"></span>` : running ? `<span class="tool-state" data-i18n="运行中">${t("运行中")}</span>` : '';
  return `<details class="tool-item ${failed ? 'failed' : ''}" data-item-id="${h(item.id)}"><summary>${icon('chevron-right', 'tool-chevron')}${running ? '<span class="spinner"></span>' : icon(glyph, 'tool-icon')}<span class="tool-title">${h(title)}</span>${status || indicator}</summary><div class="tool-content${cards ? ' tool-cards' : ''}">${content}</div></details>`;
}

const toolValue = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
const toolFinished = item => item._active === false || ['completed', 'failed', 'declined', 'cancelled', 'interrupted'].includes(item.status);

function toolCard(id, title, value) {
  return `<section class="tool-card" data-tool-card="${h(id)}"><div class="tool-card-heading">${h(title)}</div><pre>${h(toolValue(value))}</pre></section>`;
}

function toolCards(item, title, input, output) {
  const content = toolCard(`${item.id}:call`, t("工具调用"), input)
    + (output === undefined ? '' : toolCard(`${item.id}:result`, t("工具返回"), output));
  return toolItem(item, title, 'terminal', content, { cards: true });
}

function compactionStatus(item, running, outcome) {
  const end = running ? Date.now() : item.completedAtMs;
  const duration = item.startedAtMs == null || end == null ? null : Math.max(0, end - item.startedAtMs);
  const timer = duration == null ? '' : `<span class="compaction-duration"${running ? ` data-started-at="${item.startedAtMs}"` : ''}>· ${formatDuration(duration)}</span>`;
  const label = running ? t("压缩中")
    : outcome === 'fallback_growth' ? t("变多回退")
    : outcome === 'fallback_failed' || ['failed', 'interrupted'].includes(item.status) ? t("失败回退")
    : outcome === 'keep' ? t("保留原文") : t("已压缩");
  return `<span class="tool-compaction-status" role="status"><span>${label}</span>${timer}</span>`;
}

function compactionTools(item, running) {
  return (item.results || []).map(result => {
    const status = compactionStatus(item, running, result.outcome);
    const call = result.call || {};
    const id = `${item.id}:${result.sourceId}`;
    const title = [call.namespace, call.name, result.sourceId].filter(Boolean).join(' · ');
    let content = toolCard(`${id}:call`, t("工具调用"), call.arguments ?? call.input);
    for (const [field, label] of [['original', t("原始返回")], ['projected', t("压缩后返回")]]) {
      if (result[field] == null) continue;
      const output = result[field].output;
      content += toolCard(`${id}:${field}`, label, typeof output === 'string' ? output : output.map(block => block.text).join('\n'));
    }
    return toolItem({ ...item, id, _active: running }, title, 'terminal', content, { cards: true, status });
  }).join('');
}

function agentLink(id, agents, path) {
  return `<button data-open-agent="${h(id)}" data-tooltip="${h(id)}">${h(path || agents.get(id) || id)}</button>`;
}

function agentActivity(item, title, { running = false, glyph = 'git-fork', details = '' } = {}) {
  return `<div class="subagent-event" data-item-id="${h(item.id)}"><div class="subagent-activity">${running ? '<span class="spinner"></span>' : icon(glyph)}<span>${title}</span>${item.model ? `<span class="subagent-model">${h(item.model)}${item.reasoningEffort ? ` · ${h(item.reasoningEffort)}` : ''}</span>` : ''}</div>${details ? `<div class="subagent-details">${details}</div>` : ''}</div>`;
}

function collabItem(item, agents) {
  const running = item.status === 'inProgress';
  const ids = item.receiverThreadIds || [];
  const first = ids[0] ? agentLink(ids[0], agents) : '';
  const failed = ['failed', 'interrupted'].includes(item.status);
  const prompt = item.prompt ? `<div class="subagent-prompt">${h(item.prompt)}</div>` : '';
  switch (item.tool) {
    case 'sendMessage':
    case 'followupTask':
    case 'interruptAgent':
    case 'listAgents':
      return '';
    case 'spawnAgent':
      return running ? '' : agentActivity(item, first ? t('已创建子代理 {0}', first) : t('创建子代理失败'), { details: prompt });
    case 'sendInput':
      return running ? '' : agentActivity(item, t('已向 {0} 发送输入', first), { details: prompt });
    case 'resumeAgent':
      return agentActivity(item, failed ? t('恢复 {0} 失败', first) : running ? t('正在恢复 {0}', first) : t('已恢复 {0}', first), { running });
    case 'closeAgent':
      return running ? '' : agentActivity(item, failed ? t('关闭 {0} 失败', first) : t('已关闭 {0}', first));
    case 'wait': {
      if (running) {
        const title = ids.length === 1 ? t('正在等待 {0}', first) : ids.length ? t('正在等待 {0} 个子代理', ids.length) : t('正在等待子代理');
        const details = ids.length > 1 ? ids.map(id => `<div class="subagent-result">${agentLink(id, agents)}</div>`).join('') : '';
        return agentActivity(item, title, { running: true, details });
      }
      const states = item.agentsStates || {};
      const entries = [...new Set([...ids, ...Object.keys(states)])].filter(id => states[id]);
      const labels = { pendingInit: t('等待初始化'), running: t('运行中'), interrupted: t('已中断'), completed: t('已完成'), errored: t('执行出错'), shutdown: t('已关停'), notFound: t('未找到') };
      const details = entries.map(id => {
        const state = states[id];
        const message = state.message?.replace(/\s+/g, ' ').trim();
        const preview = message ? `${message.slice(0, 240)}${message.length > 240 ? '…' : ''}` : '';
        return `<div class="subagent-result">${agentLink(id, agents)}<span class="agent-state ${h(state.status)}">${h(labels[state.status] || state.status)}</span>${preview ? `<span class="agent-preview">${h(preview)}</span>` : ''}</div>`;
      }).join('') || t('尚无子代理完成');
      return agentActivity(item, failed ? t('等待已中断') : t('等待结束'), { glyph: failed ? 'circle-alert' : 'check', details });
    }
    default:
      return '';
  }
}

export function messageEditor(editing, running = false) {
  return `<form class="message-editor" data-edit-form><textarea class="message-edit-input" data-item-id="${h(editing.itemId)}" aria-label="${t("编辑消息")}" rows="3" ${editing.saving ? 'disabled' : ''} data-i18n-aria-label="编辑消息">${h(editing.text)}</textarea>${editing.error ? `<div class="message-edit-error" role="alert">${h(editing.error)}</div>` : ''}<div class="message-edit-actions"><button class="button subtle" type="button" data-cancel-edit ${editing.saving ? 'disabled' : ''} data-i18n="取消">${t("取消")}</button><button class="button primary" type="submit" ${editing.saving || (!editing.text.trim() && !editing.hasAttachments) || (editing.recovery && running) ? 'disabled' : ''}>${editing.saving ? t("正在重新发送…") : running ? t("停止并重新发送") : t("重新发送")}</button></div></form>`;
}

function userContent(item, machineId) {
  const parts = Array.isArray(item.content) ? item.content : [];
  const lines = itemText(item).split('\n');
  const filePattern = /^(?:Attached file: |附件文件：)(\/.*)$/;
  const files = lines.map(line => line.match(filePattern)?.[1]).filter(Boolean);
  const text = lines.filter(line => !filePattern.test(line)).join('\n');
  const images = parts.filter(part => ['localImage', 'image', 'input_image'].includes(part.type));
  const downloads = files.map(path => {
    return `<a class="message-file" href="${h(fileUrl(machineId, path))}" download data-tooltip="${h(path)}">${icon('file-code')}<span>${h(basename(path))}</span>${icon('download')}</a>`;
  }).join('');
  return h(text) + downloads + images.map(image => {
    const url = /^(data:image\/(png|jpeg|webp|gif);base64,|https?:\/\/)/i.test(image.url || '') ? image.url : image.path && machineId ? fileUrl(machineId, image.path, true) : '';
    const download = image.path && machineId ? `<a class="message-file" href="${h(fileUrl(machineId, image.path))}" download data-tooltip="${h(image.path)}">${icon('image')}<span>${h(basename(image.path))}</span>${icon('download')}</a>` : '';
    return url ? `<button type="button" class="message-image" data-preview-image aria-label="${t("预览图片")}" data-i18n-aria-label="预览图片"><img src="${h(url)}" alt="${t("用户图片")}" loading="lazy" data-i18n-alt="用户图片"></button>${download}` : `<div class="message-image-path">${icon('image')}${h(image.path || t("图片"))}</div>`;
  }).join('');
}

function agentMessageActions(item, { busy, editing, readOnly, turnRunning }) {
  const complete = !turnRunning || item._active === false || item.completedAtMs != null;
  if (readOnly || !complete) return '';
  return `<div class="agent-message-actions" role="group" aria-label="${t("这条消息的操作")}" data-i18n-aria-label="这条消息的操作"><button class="icon-button" data-copy-message="${h(item.id)}" data-tooltip="${t("复制这条消息")}" aria-label="${t("复制这条消息")}" data-i18n-data-tooltip="复制这条消息" data-i18n-aria-label="复制这条消息">${icon('copy')}</button><button class="icon-button" data-fork-message="${h(item.id)}" data-tooltip="${t("从消息末尾分叉")}" aria-label="${t("从消息末尾分叉")}" ${busy || editing?.saving ? 'disabled' : ''} data-i18n-data-tooltip="从消息末尾分叉" data-i18n-aria-label="从消息末尾分叉">${icon('git-fork')}</button><button class="icon-button" data-regenerate-message="${h(item.id)}" data-tooltip="${t("重新生成这条消息")}" aria-label="${t("重新生成这条消息")}" ${busy || editing ? 'disabled' : ''} data-i18n-data-tooltip="重新生成这条消息" data-i18n-aria-label="重新生成这条消息">${icon('refresh')}</button></div>`;
}

export function renderItem(item, agents = new Map(), { editing, busy = false, running = false, turnRunning = false, questionPending = false, machineId, readOnly = false } = {}) {
  switch (item.type) {
    case 'userMessage': {
      const delivery = messageDelivery(item);
      const { kind, name } = messageOrigin(item);
      const incoming = delivery || (kind && !['human', 'user'].includes(kind));
      const header = incoming ? incomingHeader(item, delivery) : null;
      const sentAt = item.startedAtMs ?? item.completedAtMs;
      const timestamp = sentAt == null ? '' : `<time class="message-time" datetime="${new Date(sentAt).toISOString()}" data-tooltip="${t("发送时间")}" data-i18n-data-tooltip="发送时间">${formatDateTime(sentAt / 1000)}</time>`;
      const retry = item._sendError ? `<button class="icon-button small message-send-error" data-retry-message="${h(item.id)}" data-tooltip="${t("发送失败：{0}。点击重新发送", h(item._sendError))}" aria-label="${t("发送失败，重新发送")}" ${busy || editing?.saving ? 'disabled' : ''} data-i18n-aria-label="发送失败，重新发送">${icon('circle-alert')}</button>` : '';
      const content = editing?.itemId === item.id && !editing.regenerating ? messageEditor(editing, running) : `<div class="user-bubble">${userContent(item, machineId)}</div>${copiedRecipients(delivery)}${readOnly ? '' : `<div class="message-actions${item._sendError ? ' has-send-error' : ''}">${timestamp}<button class="icon-button small" data-copy-message="${h(item.id)}" data-tooltip="${t("复制消息")}" aria-label="${t("复制消息")}" data-i18n-data-tooltip="复制消息" data-i18n-aria-label="复制消息">${icon('copy')}</button>${retry || `<button class="icon-button small" data-edit-message="${h(item.id)}" data-tooltip="${t("编辑消息")}" aria-label="${t("编辑消息")}" ${busy || editing?.saving || item._local ? 'disabled' : ''} data-i18n-data-tooltip="编辑消息" data-i18n-aria-label="编辑消息">${icon('pencil')}</button>`}</div>`}`;
      const label = !incoming && name ? `<div class="message-source">${icon('link')}${h(name)}</div>` : '';
      return `<div class="message user-message${incoming ? ' incoming-message' : ''}" data-message-id="${h(item.id)}"${readOnly ? '' : ' tabindex="0"'}>${header ? `${header.avatar}<div class="incoming-message-content">${header.meta}${content}</div>` : label + content}</div>`;
    }
    case 'agentMessage': {
      const streaming = turnRunning && item._active !== false && item.completedAtMs == null;
      const content = questionPending ? questionForm(item.questions, `message:${item.id}`, `data-question-item="${h(item.id)}"`, busy) : `<div class="markdown">${markdown(itemText(item), { streaming })}</div>`;
      const actions = agentMessageActions(item, { busy, editing, readOnly, turnRunning });
      return `<div class="message agent-message" data-message-id="${h(item.id)}"${actions ? ' tabindex="0"' : ''}>${content}${actions}</div>`;
    }
    case 'reasoning': {
      const text = reasoningText(item);
      return text.trim() ? toolItem(item, t("推理摘要"), 'brain', `<div class="markdown">${markdown(text, { streaming: Boolean(item._active) })}</div>`) : '';
    }
    case 'commandExecution': {
      const input = [item.command, item.cwd].filter(Boolean).join('\n');
      const output = item.aggregatedOutput || item.exitCode != null || toolFinished(item)
        ? (item.aggregatedOutput || '') + (item.exitCode != null ? t("\n\n退出码 {0}", item.exitCode) : '') : undefined;
      return toolCards(item, item.command || t("执行命令"), input, output);
    }
    case 'fileChange': {
      const changes = item.changes || [];
      const content = changes.map(change => `<div class="file-change-heading">${icon('file-code')}<span>${h(change.path)}</span></div><pre>${String(change.diff || '').split('\n').map(line => `<span class="diff-line ${line.startsWith('+') && !line.startsWith('+++') ? 'added' : line.startsWith('-') && !line.startsWith('---') ? 'removed' : ''}">${h(line)}</span>`).join('')}</pre>`).join('');
      return toolItem(item, changes.length === 1 ? t("修改 {0}", basename(changes[0].path)) : t("修改 {0} 个文件", changes.length), 'file-code', content || `<pre data-i18n="修改已记录。">${t("修改已记录。")}</pre>`);
    }
    case 'mcpToolCall':
    case 'dynamicToolCall':
    case 'functionCall': {
      const title = [item.server || item.namespace, item.tool || item.name].filter(Boolean).join(' · ') || t("工具调用");
      const result = item.error != null ? { error: item.error } : item.result ?? item.contentItems ?? item.output;
      return toolCards(item, title, item.arguments ?? item.input, result ?? (item.type !== 'functionCall' && toolFinished(item) ? '' : undefined));
    }
    case 'functionCallOutput':
      return toolItem(item, [item.namespace, item.name].filter(Boolean).join(' · ') || t("工具返回"), 'corner-down-left', toolCard(`${item.id}:result`, t("工具返回"), item.output), { cards: true });
    case 'webSearch':
      return toolItem(item, item.query ? t("搜索 {0}", item.query) : t("网页搜索"), 'globe', `<pre>${h(JSON.stringify(item.action || item, null, 2))}</pre>`);
    case 'plan':
      return toolItem(item, t("执行计划"), 'file-code', `<div class="markdown">${markdown(item.text || JSON.stringify(item.plan || item, null, 2))}</div>`);
    case 'executionPlan':
      return toolItem(item, t("执行计划"), 'file-code', `<div class="execution-plan">${item.explanation ? `<p>${h(item.explanation)}</p>` : ''}${(item.plan || []).map(step => `<div class="plan-step ${h(step.status)}">${step.status === 'completed' ? icon('check') : step.status === 'inProgress' ? '<span class="spinner"></span>' : ''}<span>${h(step.step)}</span></div>`).join('')}</div>`);
    case 'subAgentActivity': {
      const labels = { started: t('已启动'), interacted: t('已交互'), interrupted: t('已中断'), completed: t('已完成') };
      return agentActivity(item, `${h(labels[item.kind] || item.kind)} ${agentLink(item.agentThreadId, agents, item.agentPath)}`, { glyph: item.kind === 'completed' ? 'check' : 'git-fork' });
    }
    case 'collabAgentToolCall':
      return collabItem(item, agents);
    case 'enteredReviewMode':
    case 'exitedReviewMode':
      return `<div class="tool-item"><summary>${icon('check', 'tool-icon')}<span>${h({ enteredReviewMode: t("已开始代码审查"), exitedReviewMode: t("代码审查已结束") }[item.type])}</span></summary></div>`;
    case 'toolCallCompaction': {
      const running = isCompactionRunning(item, turnRunning);
      return compactionTools(item, running) || toolItem({ ...item, _active: running }, t("工具调用"), 'terminal', '', { status: compactionStatus(item, running) });
    }
    case 'contextCompaction': {
      const running = isCompactionRunning(item, turnRunning);
      const end = running ? Date.now() : item.completedAtMs;
      const duration = item.startedAtMs == null || end == null ? null : Math.max(0, end - item.startedAtMs);
      const timer = duration == null ? '' : `<span class="compaction-duration"${running ? ` data-started-at="${item.startedAtMs}"` : ''}>· ${formatDuration(duration)}</span>`;
      return `<div class="context-compaction" role="status">${running ? '<span class="spinner"></span>' : icon('check')}<span>${running ? t("正在压缩") : t("压缩完成")}</span>${timer}</div>`;
    }
    default:
      return toolItem(item, item.type || t("会话记录"), 'file-code', `<pre>${h(JSON.stringify(item, null, 2))}</pre>`);
  }
}

export function turnFooter(turn, { busy = false, editing, settings } = {}) {
  if (turn._local) return '';
  const complete = !isTurnRunning(turn);
  const duration = turnDuration(turn);
  const metric = complete && throughput.enabled(settings, turn) && throughput.average(turn);
  const completedAt = turn.completedAt == null ? '' : `<time class="turn-completed" datetime="${new Date(turn.completedAt * 1000).toISOString()}" data-tooltip="${t("结束时间")}" data-i18n-data-tooltip="结束时间">${formatDateTime(turn.completedAt)}</time>`;
  const hasReply = turn.items.some(item => item.type === 'agentMessage');
  const regeneratable = hasReply || turn.items.some(item => item.type === 'userMessage');
  const menu = `<details class="action-menu turn-menu"><summary class="icon-button" aria-label="${t("本轮操作")}" data-tooltip="${t("本轮操作")}" data-i18n-aria-label="本轮操作" data-i18n-data-tooltip="本轮操作">${icon('ellipsis')}</summary><div class="menu-popup"><button data-copy-turn="${h(turn.id)}" ${hasReply ? '' : 'disabled'}>${icon('copy')}<span data-i18n="复制本轮全部回复">${t("复制本轮全部回复")}</span></button><button data-regenerate-turn="${h(turn.id)}" ${busy || editing || !regeneratable ? 'disabled' : ''}>${icon('refresh')}<span data-i18n="从本轮开头重新生成">${t("从本轮开头重新生成")}</span></button>${hasReply ? '' : `<button data-fork-turn="${h(turn.id)}" ${busy || editing?.saving ? 'disabled' : ''}>${icon('git-fork')}<span data-i18n="从本轮末尾分叉">${t("从本轮末尾分叉")}</span></button>`}</div></details>`;
  const outcome = { completed: '', interrupted: t("已中断"), failed: t("执行未完成") }[turn.status] || '';
  return `${turn.error ? `<div class="turn-error">${h(turn.error.message || turn.error)}</div>` : ''}${complete ? `<footer class="turn-footer" aria-label="${t("本轮统计和操作")}" data-i18n-aria-label="本轮统计和操作"><div class="turn-meta">${duration == null ? '' : `<span class="turn-duration" data-tooltip="${t("本轮用时")}" data-i18n-data-tooltip="本轮用时">${t("用时 {0}", formatDuration(duration))}</span>`}${metric ? `<span class="turn-tps" data-tooltip="${t("本轮已测回复平均速度{0}", metric.estimated ? t("（估算）") : '')}">${throughput.format(metric)}</span>` : ''}${completedAt}</div>${outcome ? `<span class="turn-outcome">${h(outcome)}</span>` : ''}${menu}</footer>` : ''}`;
}

export function backgroundTasks(tasks) {
  if (!tasks.length) return `<div class="task-empty" data-i18n="暂无后台任务">${t("暂无后台任务")}</div>`;
  return tasks.map(task => `<article class="task-item"><div class="task-item-title"><i class="status-dot connected"></i><span>${t("进程 {0}", h(task.processId))}</span></div><pre class="task-command">${h(task.command)}</pre><div class="task-cwd">${h(task.cwd)}</div><button class="button subtle" data-stop-task="${h(task.processId)}">${t("{0}停止", icon('square'))}</button></article>`).join('');
}

function questionForm(questions, key, attributes, busy = false) {
  const fields = questions.map((question, index) => {
    const title = question.title || question.question || question.header;
    const options = question.options || [];
    const name = `question-${key}-${index}`;
    const choices = options.map((option, optionIndex) => {
      const label = typeof option === 'string' ? option : option.label;
      return `<label class="question-option"><input type="${question.multiSelect ? 'checkbox' : 'radio'}" name="${h(name)}" value="${h(label)}" ${optionIndex === 0 && !question.multiSelect ? 'checked' : ''}><span>${h(label)}${option.description ? `<span class="question-option-description">${h(option.description)}</span>` : ''}</span></label>`;
    }).join('');
    const other = options.length ? `<label class="question-option"><input type="${question.multiSelect ? 'checkbox' : 'radio'}" name="${h(name)}" value="" data-other-answer><span data-i18n="其他方案">${t("其他方案")}</span></label>` : '';
    const inputAttributes = `class="request-field" data-free-answer aria-label="${h(title)}" placeholder="${options.length ? t("输入其他方案…") : t("输入回答…")}" ${options.length ? 'hidden' : 'required'}`;
    const input = question.isSecret ? `<input type="password" ${inputAttributes}>` : `<textarea rows="2" ${inputAttributes}></textarea>`;
    return `<fieldset class="question" data-question-id="${h(question.id ?? index)}"><legend class="question-label">${h(title)}</legend>${options.length ? `<div class="question-options">${choices}${other}</div>` : ''}${input}</fieldset>`;
  }).join('');
  return `<form class="request-card question-card" data-question-form="${h(key)}" ${attributes}><div class="request-heading">${icon('circle-help')}<span data-i18n="Codex 有 {0} 个问题" data-i18n-values="${h(JSON.stringify([questions.length]))}">${t("Codex 有 {0} 个问题", questions.length)}</span></div><fieldset class="question-fields" ${busy ? 'disabled' : ''}>${fields}</fieldset><div class="request-actions"><button type="submit" class="button primary" ${busy ? 'disabled' : ''} data-i18n="发送回答">${t("发送回答")}</button></div></form>`;
}

export function requestCards(requests) {
  return requests.map(request => {
    const p = request.params;
    let title = t("需要回应");
    let body = '';
    let actions = '';
    const action = (label, result, style = 'subtle') => `<button class="button ${style}" data-request-result="${h(JSON.stringify(result))}">${label}</button>`;
    if (request.method.includes('requestUserInput')) {
      return questionForm(p.questions, `request:${request.id}`, `data-request-id="${h(request.id)}"`);
    } else if (request.method === 'item/permissions/requestApproval') {
      title = t("请求额外权限");
      body = `${p.reason ? `<p>${h(p.reason)}</p>` : ''}<pre>${h(JSON.stringify(p.permissions || {}, null, 2))}</pre>`;
      actions = action(t("拒绝"), { permissions: {}, scope: 'turn' }) + action(t("本会话允许"), { permissions: p.permissions, scope: 'session' }) + action(t("允许本轮"), { permissions: p.permissions, scope: 'turn' }, 'primary');
    } else if (request.method.includes('requestApproval')) {
      title = request.method.includes('fileChange') ? t("请求修改文件") : t("请求执行命令");
      body = `${p.reason ? `<p>${h(p.reason)}</p>` : ''}${p.command ? `<pre>${h(p.command)}</pre>` : ''}${p.cwd || p.grantRoot ? `<p class="empty-inline">${h(p.cwd || p.grantRoot)}</p>` : ''}`;
      const decisions = p.availableDecisions || ['accept', 'acceptForSession', 'decline', 'cancel'];
      actions = (decisions.includes('decline') ? action(t("拒绝"), { decision: 'decline' }) : '')
        + (decisions.includes('cancel') ? action(t("取消执行"), { decision: 'cancel' }) : '')
        + (decisions.includes('acceptForSession') ? action(t("本会话允许"), { decision: 'acceptForSession' }) : '')
        + decisions.filter(decision => typeof decision === 'object').map(decision => action(decision.acceptWithExecpolicyAmendment ? t("允许并保存命令规则") : t("应用建议规则"), { decision })).join('')
        + (decisions.includes('accept') ? action(t("允许"), { decision: 'accept' }, 'primary') : '');
    } else if (request.method === 'mcpServer/elicitation/request') {
      title = t("工具需要补充信息");
      const properties = p.requestedSchema?.properties || {};
      const field = (key, value) => {
        const attrs = `name="${h(key)}" data-elicitation-key="${h(key)}"`;
        let input;
        if (value.enum) input = `<select ${attrs} data-elicitation-type="json">${value.enum.map(option => `<option value="${h(JSON.stringify(option))}">${h(option)}</option>`).join('')}</select>`;
        else if (value.type === 'boolean') input = `<select ${attrs} data-elicitation-type="json"><option value="true" data-i18n="是">${t("是")}</option><option value="false" data-i18n="否">${t("否")}</option></select>`;
        else if (value.type === 'number' || value.type === 'integer') input = `<input ${attrs} type="number" value="${h(value.default)}" placeholder="${h(value.description || '')}">`;
        else if (value.type === 'object' || value.type === 'array') input = `<textarea ${attrs} data-elicitation-type="json">${h(JSON.stringify(value.default ?? (value.type === 'array' ? [] : {}), null, 2))}</textarea>`;
        else input = `<input ${attrs} type="text" value="${h(value.default)}" placeholder="${h(value.description || '')}">`;
        return `<label class="field"><span>${h(value.title || key)}</span>${input}</label>`;
      };
      body = `<p>${h(p.message || '')}</p>${p.url ? `<p><a href="${h(p.url)}" target="_blank" rel="noopener noreferrer" data-i18n="打开工具页面 ↗">${t("打开工具页面 ↗")}</a></p>` : ''}${Object.entries(properties).map(([key, value]) => field(key, value)).join('')}${p.requestedSchema ? `<details class="advanced-fields"><summary data-i18n="以 JSON 填写">${t("以 JSON 填写")}</summary><textarea class="request-field" data-elicitation-raw rows="4" placeholder="{}"></textarea></details>` : ''}`;
      actions = action(t("取消"), { action: 'cancel' }) + `<button class="button primary" data-answer-elicitation data-i18n="发送">${t("发送")}</button>`;
    } else {
      title = p.tool || request.method;
      body = `<pre>${h(JSON.stringify(p, null, 2))}</pre><label class="field"><span data-i18n="返回结果">${t("返回结果")}</span><textarea class="request-field" data-json-result rows="4">{}</textarea></label>`;
      actions = `<button class="button primary" data-answer-json data-i18n="提交结果">${t("提交结果")}</button>`;
    }
    return `<article class="request-card" data-request-id="${h(request.id)}"><div class="request-heading">${icon('circle-help')}<span>${h(title)}</span></div><div class="request-body">${body}</div><div class="request-actions">${actions}</div></article>`;
  }).join('');
}

export function machineCards(machines, pending = new Set()) {
  if (!machines.length) return `<p class="settings-note" data-i18n="暂无机器">${t("暂无机器")}</p>`;
  const labels = { connected: t("已连接"), disconnected: t("未连接"), connecting: t("连接中"), reconnecting: t("正在重连"), error: t("连接出错") };
  return machines.map(machine => {
    const connecting = ['connected', 'connecting', 'reconnecting'].includes(machine.status);
    const disabled = pending.has(machine.id) ? 'disabled' : '';
    return `<article class="machine-card"><div class="machine-card-heading">${icon(machine.transport === 'ssh' ? 'server' : 'monitor')}<strong>${h(machine.name)}</strong><span class="machine-card-status"><i class="status-dot ${h(machine.status)}"></i>${h(labels[machine.status] || machine.status)}</span></div><div class="machine-card-meta">${h(machine.transport === 'ssh' ? `${machine.user ? `${machine.user}@` : ''}${machine.host}${machine.port && machine.port !== 22 ? `:${machine.port}` : ''}` : t("本机"))}${machine.version ? ` · Codex ${h(machine.version)}` : ''}</div><div class="machine-card-actions"><button type="button" class="button subtle" data-machine-action="${connecting ? 'disconnect' : 'connect'}" data-machine-id="${h(machine.id)}" ${disabled}>${connecting ? t("断开连接") : t("连接")}</button><button type="button" class="button subtle" data-machine-action="reconnect" data-machine-id="${h(machine.id)}" ${disabled} data-i18n="重新连接">${t("重新连接")}</button><button type="button" class="button subtle" data-machine-find="${h(machine.id)}" data-i18n="查找会话">${t("查找会话")}</button><button type="button" class="button subtle" data-machine-edit="${h(machine.id)}" data-i18n="编辑">${t("编辑")}</button><button type="button" class="icon-button" data-machine-delete="${h(machine.id)}" aria-label="${t("移除 {0}", h(machine.name))}" data-tooltip="${t("移除机器配置")}" data-i18n-data-tooltip="移除机器配置">${icon('trash')}</button></div>${machine.error ? `<div class="machine-error">${h(machine.error)}</div>` : ''}</article>`;
  }).join('');
}
