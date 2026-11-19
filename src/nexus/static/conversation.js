import { t, locale } from './i18n.js?v=145';
import { renderItem, turnFooter, messageEditor, isTurnRunning, formatDuration } from './views.js?v=145';
import { filePreviewUrl, localFilePath } from './files.js?v=145';
import { icon } from './icons.js?v=145';
import { mountVisualizations, patchVisualizationMarkdown } from './visualizations.js?v=145';
import { mountMermaid, preserveMermaid } from './mermaid.js?v=145';

const toolTypes = new Set(['commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall', 'functionCall', 'functionCallOutput', 'toolCallCompaction', 'webSearch', 'imageView', 'imageGeneration', 'collabAgentToolCall', 'collabToolCall']);
const hoverActions = matchMedia('(hover: hover)');

let gesture;
document.addEventListener('pointerdown', event => {
  document.documentElement.classList.remove('keyboard-navigation');
  gesture = {
    id: event.pointerId, x: event.clientX, y: event.clientY, started: event.timeStamp,
    cancelled: event.button !== 0 || window.getSelection()?.isCollapsed === false,
  };
}, { capture: true });
document.addEventListener('pointermove', event => {
  if (gesture?.id === event.pointerId && Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) > 10) gesture.cancelled = true;
});
document.addEventListener('pointerup', event => {
  if (gesture?.id === event.pointerId) gesture.valid = !gesture.cancelled && event.timeStamp - gesture.started < 500;
});
for (const name of ['pointercancel', 'contextmenu']) document.addEventListener(name, () => {
  if (gesture) { gesture.cancelled = true; gesture.valid = false; }
});
document.addEventListener('click', event => {
  if (!hoverActions.matches && (event.detail > 0 || event.pointerType) && !gesture?.valid) return;
  document.querySelectorAll('.turn-menu[open]').forEach(menu => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  if (hoverActions.matches) return;
  gesture = null;
  const message = event.target.closest('.conversation .message[tabindex]');
  if (message && event.target.closest('button, a, input, textarea, select, summary')) return;
  if (window.getSelection()?.isCollapsed === false) return;
  const opening = message && !message.classList.contains('actions-open');
  document.querySelectorAll('.message.actions-open').forEach(node => node.classList.remove('actions-open'));
  if (opening) message.classList.add('actions-open');
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') document.querySelectorAll('.turn-menu[open]').forEach(menu => {
    menu.open = false;
    menu.querySelector('summary').focus();
  });
  if (event.key === 'Tab') document.documentElement.classList.add('keyboard-navigation');
  if (!hoverActions.matches && event.target.matches('.conversation .message[tabindex]') && ['Enter', ' '].includes(event.key)) {
    event.preventDefault();
    event.target.click();
  }
});
document.addEventListener('toggle', event => {
  if (!event.target.matches('.turn-menu[open]')) return;
  const popup = event.target.querySelector('.menu-popup');
  popup.style.top = '';
  popup.style.bottom = '';
  const scroller = event.target.closest('.conversation-scroll');
  if (scroller && popup.getBoundingClientRect().top < scroller.getBoundingClientRect().top) {
    popup.style.top = 'calc(100% + 8px)';
    popup.style.bottom = 'auto';
  }
}, true);

function itemSegments(turn, rendered) {
  const segments = [];
  let items = [];
  const flush = closed => {
    if (!items.length) return;
    const firstTool = items.find(item => toolTypes.has(item.type) && rendered.get(item.id).node.hasChildNodes());
    const complete = items.every(item => !(item._active ?? ['inProgress', 'running'].includes(item.status)));
    segments.push({ items, groupId: closed && complete && firstTool ? firstTool.id : null });
    items = [];
  };
  for (const item of turn.items || []) {
    const empty = !rendered.get(item.id).node.hasChildNodes();
    if (toolTypes.has(item.type) || item.type === 'reasoning' || empty) items.push(item);
    else {
      flush(true);
      segments.push({ items: [item] });
    }
  }
  flush(!isTurnRunning(turn));
  return segments;
}

function context(turns, threads) {
  const agents = new Map();
  const pendingQuestions = new Set();
  for (const thread of threads) {
    const path = thread.source?.subAgent?.thread_spawn?.agent_path;
    if (path) agents.set(thread.id, path);
  }
  for (const turn of turns) for (const item of turn.items || []) {
    if (item.type === 'subAgentActivity') agents.set(item.agentThreadId, item.agentPath);
    if (item.type === 'userMessage') pendingQuestions.clear();
    else if (item.type === 'agentMessage' && item.questions?.length) pendingQuestions.add(item.id);
  }
  return { agents, pendingQuestions, agentLabels: JSON.stringify([...agents]) };
}

function patchContents(node, html, { machineId, cwd } = {}) {
  const currentMessage = node.querySelector('.agent-message');
  const currentMarkdown = currentMessage?.querySelector(':scope > .markdown');
  if (currentMarkdown?.querySelector('[data-visualization-path]') && node.visualizationMachine === machineId) {
    const next = document.createElement('template');
    next.innerHTML = html;
    const nextMessage = next.content.querySelector('.agent-message');
    const nextMarkdown = nextMessage?.querySelector(':scope > .markdown');
    if (nextMarkdown) {
      preserveMermaid(currentMarkdown, nextMarkdown);
      patchVisualizationMarkdown(currentMarkdown, nextMarkdown);
      const actions = nextMessage.querySelector('.agent-message-actions');
      const previousActions = currentMessage.querySelector('.agent-message-actions');
      if (actions?.outerHTML !== previousActions?.outerHTML) {
        previousActions?.remove();
        if (actions) currentMessage.append(actions);
      }
      currentMessage.toggleAttribute('tabindex', nextMessage.hasAttribute('tabindex'));
      if (nextMessage.hasAttribute('tabindex')) currentMessage.tabIndex = 0;
      linkFiles(currentMarkdown, machineId, cwd);
      return;
    }
  }
  node.visualizationMachine = machineId;
  const details = new Set([...node.querySelectorAll('details[open]')].map(detail => detail.dataset.itemId));
  const forms = new Map([...node.querySelectorAll('[data-question-form]')].map(form => [form.dataset.questionForm, form]));
  const editor = node.querySelector('.message-edit-input');
  const actionsOpen = node.querySelector('.message.actions-open')?.dataset.messageId;
  const focused = node.contains(document.activeElement) ? document.activeElement : null;
  const next = document.createElement('template');
  next.innerHTML = html;
  preserveMermaid(node, next.content);
  const cards = new Map([...node.querySelectorAll('details.tool-item[data-item-id]')].map(card => [card.dataset.itemId, card]));
  const sections = new Map([...node.querySelectorAll('[data-tool-card]')].map(card => [card.dataset.toolCard, card]));
  const scrollKey = pre => pre.closest('[data-tool-card]')?.dataset.toolCard
    || `${pre.closest('details').dataset.itemId}:${[...pre.closest('details').querySelectorAll('pre')].indexOf(pre)}`;
  const scrolls = new Map([...node.querySelectorAll('details.tool-item pre')].map(pre => [scrollKey(pre), pre.scrollTop]));
  next.content.querySelectorAll('details.tool-item[data-item-id]').forEach(card => {
    const previous = cards.get(card.dataset.itemId);
    if (!previous) return;
    card.open = previous.open;
    if (previous.isEqualNode(card)) card.replaceWith(previous);
  });
  next.content.querySelectorAll('[data-tool-card]').forEach(card => {
    const previous = sections.get(card.dataset.toolCard);
    if (previous && previous.isEqualNode(card)) card.replaceWith(previous);
  });
  node.replaceChildren(next.content);
  node.querySelectorAll('details.tool-item[data-item-id] pre').forEach(pre => {
    const scroll = scrolls.get(scrollKey(pre));
    if (scroll != null) pre.scrollTop = scroll;
  });
  node.querySelectorAll('.message').forEach(message => {
    if (message.dataset.messageId === actionsOpen) message.classList.add('actions-open');
  });
  node.querySelectorAll('details').forEach(detail => { if (details.has(detail.dataset.itemId)) detail.open = true; });
  node.querySelectorAll('[data-question-form]').forEach(form => {
    const previous = forms.get(form.dataset.questionForm);
    if (previous) form.replaceWith(previous);
  });
  const input = node.querySelector('.message-edit-input');
  if (input && editor && input.dataset.itemId === editor.dataset.itemId) {
    editor.disabled = input.disabled;
    input.replaceWith(editor);
  }
  if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  linkFiles(node, machineId, cwd);
}

function linkFiles(node, machineId, cwd) {
  node.querySelectorAll('a[href]').forEach(anchor => {
    if (anchor.closest('.visualization, .mermaid-block')) return;
    const path = machineId && !anchor.hasAttribute('download') && localFilePath(`${cwd?.replace(/\/$/, '') || ''}/`, anchor.getAttribute('href'));
    if (path) {
      anchor.href = filePreviewUrl(machineId, path);
      anchor.removeAttribute('target');
      anchor.removeAttribute('rel');
    } else { anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; }
  });
}

function place(parent, node, before) {
  if (node !== before) parent.insertBefore(node, before);
  return node.nextSibling;
}

function placeAfter(parent, node, previous) {
  place(parent, node, previous ? previous.nextSibling : parent.firstChild);
  return node;
}

export class ConversationRenderer {
  constructor(container) {
    this.container = container;
    this.turns = new Map();
    this.dirty = new Map();
    this.recovery = document.createElement('div');
    this.recovery.className = 'conversation-item';
  }

  tick(now = Date.now()) {
    for (const timer of this.container.querySelectorAll('.compaction-duration[data-started-at]')) {
      const text = `· ${formatDuration(Math.max(0, now - Number(timer.dataset.startedAt)))}`;
      if (timer.textContent !== text) timer.textContent = text;
    }
  }

  invalidate(turnId, itemId) {
    if (!turnId) return;
    if (!itemId) this.dirty.set(turnId, null);
    else if (!this.dirty.has(turnId) || this.dirty.get(turnId)) {
      if (!this.dirty.has(turnId)) this.dirty.set(turnId, new Set());
      this.dirty.get(turnId).add(itemId);
    }
  }

  render(turns, threads, options) {
    const { agents, pendingQuestions, agentLabels } = context(turns, threads);
    const retained = new Set();
    let nextTurn = this.container.firstChild;
    let editingFound = false;
    for (const turn of turns) {
      retained.add(turn.id);
      let entry = this.turns.get(turn.id);
      if (!entry) {
        const node = document.createElement('article');
        node.className = 'turn';
        node.dataset.turnId = turn.id;
        const footer = document.createElement('div');
        footer.className = 'conversation-item';
        node.append(footer);
        entry = { node, footer, items: new Map(), groups: new Map() };
        this.turns.set(turn.id, entry);
      }
      nextTurn = place(this.container, entry.node, nextTurn);
      const items = new Set();
      const turnRunning = isTurnRunning(turn);
      for (const item of turn.items || []) {
        items.add(item.id);
        let rendered = entry.items.get(item.id);
        if (!rendered) {
          const node = document.createElement('div');
          node.className = 'conversation-item';
          rendered = { node };
          entry.items.set(item.id, rendered);
        }
        const editing = options.editing?.itemId === item.id ? options.editing : null;
        editingFound ||= Boolean(editing);
        const questionPending = !options.readOnly && pendingQuestions.has(item.id);
        const controls = ['userMessage', 'agentMessage'].includes(item.type);
        const signature = JSON.stringify([
          locale(), options.machineId, options.cwd,
          editing, controls && options.busy, controls && options.running, controls && Boolean(options.editing), controls && options.editing?.saving, turnRunning, questionPending, options.readOnly,
          ['subAgentActivity', 'collabAgentToolCall'].includes(item.type) ? agentLabels : '',
        ]);
        const dirty = this.dirty.has(turn.id) && (!this.dirty.get(turn.id) || this.dirty.get(turn.id).has(item.id));
        if (dirty || rendered.item !== item || rendered.signature !== signature) {
          patchContents(rendered.node, renderItem(item, agents, { ...options, questionPending, turnRunning }), options);
          rendered.item = item;
          rendered.signature = signature;
        }
      }
      const groups = new Set();
      let previous = null;
      for (const segment of itemSegments(turn, entry.items)) {
        if (segment.groupId) {
          groups.add(segment.groupId);
          let group = entry.groups.get(segment.groupId);
          if (!group) {
            const node = document.createElement('details');
            node.className = 'tool-item tool-group';
            node.dataset.toolGroup = segment.groupId;
            node.innerHTML = `<summary>${icon('chevron-right', 'tool-chevron')}${icon('terminal', 'tool-icon')}<span class="tool-title" data-i18n="运行了命令">${t("运行了命令")}</span></summary><div class="tool-group-items"></div>`;
            group = { node, content: node.querySelector('.tool-group-items') };
            entry.groups.set(segment.groupId, group);
          }
          let previousTool = null;
          for (const item of segment.items) previousTool = placeAfter(group.content, entry.items.get(item.id).node, previousTool);
          previous = placeAfter(entry.node, group.node, previous);
        } else {
          for (const item of segment.items) previous = placeAfter(entry.node, entry.items.get(item.id).node, previous);
        }
      }
      for (const [id, group] of entry.groups) if (!groups.has(id)) {
        group.node.remove();
        entry.groups.delete(id);
      }
      for (const [id, item] of entry.items) if (!items.has(id)) {
        item.node.remove();
        entry.items.delete(id);
      }
      const footer = options.readOnly ? '' : turnFooter(turn, options);
      if (footer !== entry.footerHTML) {
        const menuOpen = entry.footer.querySelector('.turn-menu')?.open;
        entry.footer.innerHTML = footer;
        const menu = entry.footer.querySelector('.turn-menu');
        if (menu) menu.open = Boolean(menuOpen);
        entry.footerHTML = footer;
      }
    }
    for (const [id, turn] of this.turns) if (!retained.has(id)) {
      turn.node.remove();
      this.turns.delete(id);
    }
    const recoveryHTML = options.editing && !options.editing.regenerating && !editingFound ? messageEditor(options.editing, options.running) : '';
    if (recoveryHTML) {
      if (this.recoveryHTML !== recoveryHTML) patchContents(this.recovery, recoveryHTML);
      this.container.append(this.recovery);
    } else this.recovery.remove();
    this.recoveryHTML = recoveryHTML;
    this.dirty.clear();
    mountMermaid(this.container);
    mountVisualizations(this.container, options);
  }
}
