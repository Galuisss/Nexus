import * as throughput from './throughput.js?v=145';

export function mergeTurn(existing, snapshot) {
  if (!existing) return snapshot;
  const inProgress = ['inProgress', 'active'].includes(snapshot.status);
  const full = snapshot.itemsView === 'full' || (!snapshot.itemsView && !inProgress);
  const oldItems = new Map((existing.items || []).map(item => [item.id, item]));
  const snapshotItems = new Map((snapshot.items || []).map(item => [item.id, item]));
  // Summaries group items by type; only full snapshots define message order.
  const orderedItems = full ? snapshot.items || [] : [
    ...(existing.items || []).map(item => snapshotItems.get(item.id) || item),
    ...(snapshot.items || []).filter(item => !oldItems.has(item.id)),
  ];
  const items = orderedItems.map(item => {
    const old = oldItems.get(item.id);
    if (!old) return item;
    if (!inProgress) {
      throughput.finish(old);
      const merged = { ...item, ...(old._throughput ? { _throughput: old._throughput } : {}), ...(old.nexusTps && !Object.hasOwn(item, 'nexusTps') ? { nexusTps: old.nexusTps } : {}) };
      return JSON.stringify(merged) === JSON.stringify(old) ? old : merged;
    }
    const merged = { ...old, ...item };
    if (['contextCompaction', 'toolCallCompaction'].includes(item.type) && item.completedAtMs != null) merged._active = false;
    for (const field of ['text', 'aggregatedOutput']) {
      if (old[field]?.length > (item[field]?.length || 0)) merged[field] = old[field];
    }
    if (old.summary?.length && !item.summary?.length) merged.summary = old.summary;
    if (old.content?.length && !item.content?.length) merged.content = old.content;
    return JSON.stringify(merged) === JSON.stringify(old) ? old : merged;
  });
  if (full && (inProgress || existing.items?.some(item => item._ephemeral))) {
    const ids = new Set(items.map(item => item.id));
    for (const [index, item] of (existing.items || []).entries()) {
      if (ids.has(item.id) || (full && !inProgress && !item._ephemeral)) continue;
      const next = existing.items.slice(index + 1).find(candidate => ids.has(candidate.id));
      const position = next ? items.findIndex(candidate => candidate.id === next.id) : items.length;
      items.splice(position, 0, item);
      ids.add(item.id);
    }
  }
  const merged = { ...existing, ...snapshot, items };
  const { items: oldItemsList, ...before } = existing;
  const { items: newItemsList, ...after } = merged;
  return items.length === (existing.items || []).length && items.every((item, index) => item === existing.items[index])
    && JSON.stringify(before) === JSON.stringify(after) ? existing : merged;
}

function eventTurn(turns, turnId) {
  let turn = turns.find(turn => turn.id === turnId);
  if (!turn) { turn = { id: turnId, status: 'inProgress', items: [] }; turns.push(turn); }
  return turn;
}

export function applyTurnEvent(turns, method, p, settings) {
  if (method === 'turn/started') {
    const turn = eventTurn(turns, p.turn.id);
    Object.assign(turn, mergeTurn(turn, p.turn));
  } else if (method === 'turn/completed') {
    const turn = eventTurn(turns, p.turn.id);
    Object.assign(turn, mergeTurn(turn, p.turn));
    turn.items.forEach(item => { item._active = false; throughput.finish(item); });
  } else if (method === 'item/started' || method === 'item/completed') {
    const turn = eventTurn(turns, p.turnId);
    const index = turn.items.findIndex(item => item.id === p.item.id);
    const item = { ...p.item, _active: method === 'item/started' };
    if (p.startedAtMs != null) item.startedAtMs = p.startedAtMs;
    if (p.completedAtMs != null) item.completedAtMs = p.completedAtMs;
    if (index >= 0) {
      const previous = turn.items[index];
      turn.items[index] = { ...previous, ...item };
      if (method === 'item/started') {
        for (const field of ['text', 'aggregatedOutput']) {
          if (previous[field]?.length > (item[field]?.length || 0)) turn.items[index][field] = previous[field];
        }
        if (previous.summary?.length && !item.summary?.length) turn.items[index].summary = previous.summary;
        if (previous.content?.length && !item.content?.length) turn.items[index].content = previous.content;
      }
    }
    else turn.items.push(item);
    if (method === 'item/completed') throughput.finish(turn.items.find(candidate => candidate.id === p.item.id));
  } else if (['item/agentMessage/delta', 'item/commandExecution/outputDelta', 'item/reasoning/summaryTextDelta', 'item/reasoning/textDelta'].includes(method)) {
    const turn = eventTurn(turns, p.turnId);
    let item = turn.items.find(item => item.id === p.itemId);
    if (!item) {
      item = { id: p.itemId, type: method.includes('agentMessage') ? 'agentMessage' : method.includes('commandExecution') ? 'commandExecution' : 'reasoning', text: '', status: 'inProgress', _active: true };
      turn.items.push(item);
    }
    if (method.includes('agentMessage')) {
      item.text = (item.text || '') + p.delta;
      if (throughput.enabled(settings, turn)) throughput.recordDelta(item, p.delta);
    }
    else if (method.includes('commandExecution')) item.aggregatedOutput = (item.aggregatedOutput || '') + p.delta;
    else if (method === 'item/reasoning/textDelta') {
      item.content ||= [''];
      const part = item.content[p.contentIndex || 0];
      item.content[p.contentIndex || 0] = (typeof part === 'string' ? part : part?.text || '') + p.delta;
    } else {
      item.summary ||= [''];
      const part = item.summary[p.summaryIndex || 0];
      item.summary[p.summaryIndex || 0] = (typeof part === 'string' ? part : part?.text || '') + p.delta;
    }
  } else if (method === 'item/reasoning/summaryPartAdded') {
    const turn = eventTurn(turns, p.turnId);
    let item = turn.items.find(item => item.id === p.itemId);
    if (!item) { item = { id: p.itemId, type: 'reasoning', summary: [], content: [], _active: true }; turn.items.push(item); }
    item.summary[p.summaryIndex || 0] ||= '';
  } else if (method === 'turn/plan/updated') {
    const turn = eventTurn(turns, p.turnId);
    const id = `plan-${p.turnId}`;
    const plan = { id, type: 'executionPlan', plan: p.plan, explanation: p.explanation, _ephemeral: true };
    const index = turn.items.findIndex(item => item.id === id);
    if (index >= 0) turn.items[index] = plan;
    else turn.items.push(plan);
  } else if (method === 'item/mcpToolCall/progress') {
    const turn = eventTurn(turns, p.turnId);
    const item = turn.items.find(item => item.id === p.itemId);
    if (item) item.progressMessage = p.message;
  } else return false;
  return true;
}
