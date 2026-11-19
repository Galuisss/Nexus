import { api, query, threadPath } from './api.js?v=145';
import { applyTurnEvent, mergeTurn } from './conversation-state.js?v=145';
import * as throughput from './throughput.js?v=145';

// Each pane owns a controller; selection changes and history invalidation
// discard pending reads without changing drafts or other pane state.
export class ConversationController {
  constructor({ onSnapshot, onEvent, onChange, onError }) {
    Object.assign(this, { onSnapshot, onEvent, onChange, onError });
    this.generation = 0;
    this.submissions = new Map();
    this.select(null);
  }

  select(selection) {
    this.invalidate();
    this.selection = selection;
    this.turns = [];
    this.compactions = null;
    this.settings = {};
    this.goal = null;
    this.goalSupported = true;
    this.cursor = null;
    this.eventCursor = null;
    this.epoch = null;
    this.pending = [];
    this.loading = Boolean(selection);
    this.buffering = Boolean(selection);
    return selection;
  }

  get path() { return threadPath(this.selection.machineId, this.selection.threadId); }

  get visibleTurns() {
    const turns = [...this.turns];
    if (!this.selection) return turns;
    for (const submission of this.submissions.values()) {
      if (submission.path !== this.path) continue;
      const index = turns.findIndex(turn => turn.id === submission.turnId);
      if (index >= 0) {
        const turn = turns[index];
        turns[index] = { ...turn, items: [...turn.items, submission.item] };
      } else {
        turns.push({ id: `local:${submission.item.id}`, status: 'pending', items: [submission.item], _local: true });
      }
    }
    return turns;
  }

  reconcileSubmissions() {
    if (!this.submissions.size) return;
    for (const turn of this.turns) for (const item of turn.items) {
      if (item.type !== 'userMessage') continue;
      const id = item.clientId || item.id;
      if (this.submissions.get(id)?.path === this.path) this.submissions.delete(id);
    }
  }

  invalidate({ clear = false } = {}) {
    clearTimeout(this.refreshTimer);
    this.generation += 1;
    this.request = null;
    this.buffering = true;
    if (clear) {
      this.turns = [];
      this.compactions = null;
      this.cursor = null;
    }
  }

  refresh({ reload = false, clear = false } = {}) {
    if (!this.selection) return Promise.resolve();
    if (this.request && !reload) return this.request;
    this.invalidate({ clear });
    this.request = this.readSnapshot(this.generation, this.loading);
    return this.request;
  }

  async readSnapshot(generation, initial) {
    let changed = false;
    try {
      const result = await api(`${this.path}/snapshot${query({ limit: Math.max(30, this.turns.length) })}`);
      if (generation !== this.generation) return;
      const settingsChanged = JSON.stringify(this.settings) !== JSON.stringify(result.settings);
      this.settings = result.settings;
      this.goal = result.goal ?? null;
      this.goalSupported = result.goalSupported !== false;
      const previous = new Map(this.turns.map(turn => [turn.id, turn]));
      const turns = [...result.data].reverse().map(turn => mergeTurn(previous.get(turn.id), turn));
      changed = settingsChanged || result.nextCursor !== this.cursor || turns.length !== this.turns.length
        || turns.some((turn, index) => turn !== this.turns[index]);
      this.turns = turns;
      this.reconcileSubmissions();
      this.compactions = new Set(result.compactionIds);
      this.cursor = result.nextCursor;
      this.eventCursor = result.eventCursor;
      this.epoch = result.eventCursor.epoch;
      this.onSnapshot(result);
      return result;
    } catch (error) {
      if (generation === this.generation) throw error;
    } finally {
      if (generation === this.generation) {
        this.request = null;
        this.loading = false;
        this.buffering = false;
        const pending = this.pending;
        this.pending = [];
        for (const event of pending) this.handleEvent(event);
        if (generation === this.generation) this.onChange({ initial, changed });
      }
    }
  }

  scheduleRefresh({ reload = false, clear = false } = {}) {
    if (!this.selection) return;
    if (reload) this.invalidate({ clear });
    clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refresh().catch(this.onError), 220);
    if (clear) this.onChange({ initial: false, changed: true });
  }

  async older() {
    if (!this.selection || !this.cursor || this.buffering) return false;
    const generation = this.generation;
    try {
      const result = await api(`${this.path}/history${query({ cursor: this.cursor, limit: 30 })}`);
      if (generation !== this.generation) return false;
      const ids = new Set(this.turns.map(turn => turn.id));
      this.turns.unshift(...[...result.data].reverse().filter(turn => !ids.has(turn.id)));
      this.cursor = result.nextCursor;
      this.onChange({ initial: false, changed: true });
      return true;
    } catch (error) {
      if (generation === this.generation) throw error;
      return false;
    }
  }

  handleEvent(event) {
    if (!this.selection) return;
    const restarted = this.epoch && event.epoch !== this.epoch;
    this.epoch = event.epoch;
    if (event.method === 'nexus/resync' || restarted) {
      this.eventCursor = null;
      this.pending = [];
      this.refresh({ reload: true }).catch(this.onError);
      return;
    }
    const p = event.params || {};
    if (event.machineId !== this.selection.machineId
        || (p.threadId || p.thread?.id) !== this.selection.threadId) return;
    if (event.method === 'nexus/history/changed') {
      this.refresh({ reload: true }).catch(this.onError);
      return;
    }
    if (this.buffering) {
      this.pending.push(event);
      return;
    }
    if (this.eventCursor && event.id <= this.eventCursor.id) return;
    this.eventCursor = { epoch: event.epoch, id: event.id };
    if (event.origin && p.item) p.item.origin = event.origin;
    const wasEnabled = throughput.enabled(this.settings);
    if (event.method === 'thread/settings/updated') this.settings = { ...this.settings, ...p.threadSettings };
    if (event.method === 'thread/goal/updated') { this.goal = p.goal; this.goalSupported = true; }
    if (event.method === 'thread/goal/cleared') this.goal = null;
    if (wasEnabled && !throughput.enabled(this.settings)) {
      for (const turn of this.turns) for (const item of turn.items) delete item._throughput;
    }
    const turnChanged = applyTurnEvent(this.turns, event.method, p, this.settings);
    if (turnChanged && (p.turn || p.item?.type === 'userMessage')) this.reconcileSubmissions();
    if (event.method === 'item/completed' && p.item.type === 'contextCompaction') this.compactions?.add(p.item.id);
    if (event.method === 'turn/completed') {
      for (const item of p.turn.items) if (item.type === 'contextCompaction') this.compactions?.add(item.id);
    }
    this.onEvent(event, turnChanged);
    if (event.method === 'thread/reverted') this.scheduleRefresh({ reload: true, clear: true });
  }
}
