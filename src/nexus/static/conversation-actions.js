import { t } from './i18n.js?v=145';
import { api } from './api.js?v=145';
import { fileAttachment, uploadFiles } from './files.js?v=145';
import { isRunning, isTurnRunning, itemText, messageDelivery, messageOrigin, requestCards } from './views.js?v=145';

export const draftStorageKey = key => `nexus-draft:${key}`;

export function renderRequestCards(container, requests) {
  const forms = new Map([...container.querySelectorAll('[data-question-form]')].map(form => [form.dataset.questionForm, form]));
  const bodies = new Map([...container.querySelectorAll('[data-request-id]')]
    .filter(card => card.querySelector('[data-elicitation-key], [data-elicitation-raw], [data-json-result]'))
    .map(card => [card.dataset.requestId, card.querySelector('.request-body')]));
  const focused = container.contains(document.activeElement) ? document.activeElement : null;
  container.innerHTML = requestCards(requests);
  container.querySelectorAll('[data-request-id]').forEach(card => {
    const body = bodies.get(card.dataset.requestId);
    if (body) card.querySelector('.request-body').replaceWith(body);
  });
  container.querySelectorAll('[data-question-form]').forEach(form => {
    const previous = forms.get(form.dataset.questionForm);
    if (previous) form.replaceWith(previous);
  });
  container.querySelectorAll('[data-question-form]').forEach(form => setQuestionBusy(form, form.hasAttribute('data-submitting')));
  if (focused?.isConnected) focused.focus({ preventScroll: true });
}

export function setQuestionBusy(form, busy) {
  const submitted = form.hasAttribute('data-submitted');
  form.querySelector('.question-fields').disabled = busy || submitted;
  const button = form.querySelector('[type="submit"]');
  button.disabled = busy || submitted;
  button.textContent = submitted ? t("已发送") : busy ? t("正在发送…") : t("发送回答");
}

export function bindQuestionInputs(container, onChange) {
  container.addEventListener('change', event => {
    const question = event.target.closest('[data-question-id]');
    const other = question?.querySelector('[data-other-answer]');
    if (!other) return;
    const input = question.querySelector('[data-free-answer]');
    input.hidden = !other.checked;
    input.required = other.checked;
    input.setCustomValidity('');
    if (other.checked && event.target === other) input.focus();
  });
  container.addEventListener('input', event => {
    if (event.target.matches('[data-free-answer]')) event.target.setCustomValidity('');
  });
  container.addEventListener('compositionstart', event => event.target.closest('[data-question-form]')?.setAttribute('data-composing', ''));
  container.addEventListener('compositionend', event => {
    const form = event.target.closest('[data-question-form]');
    if (form) { form.removeAttribute('data-composing'); onChange(); }
  });
}

// Each pane supplies its state and rendering callbacks; submissions retain
// their original selection, draft and attachments while the user navigates.
export class ConversationActions {
  constructor({ conversation, state, input, transcript, thread, onChange, onCommit, onSettings, refreshRequests }) {
    Object.assign(this, { conversation, state, input, transcript, thread, onChange, onCommit, onSettings, refreshRequests });
    state.sending = false;
    state.settingsSaving = false;
    state.uploadingFiles = 0;
    state.editing = null;
    state.attachments ||= new Map();
    state.skillInputs ||= new Map();
    this.bindEditor();
  }

  get key() {
    const selection = this.conversation.selection;
    return selection && `${selection.machineId}/${selection.threadId}`;
  }

  get running() { return isRunning(this.thread()) || this.conversation.turns.some(isTurnRunning); }

  context() {
    return { selection: this.conversation.selection, path: this.conversation.path, key: this.key, archived: Boolean(this.thread()?.archived) };
  }

  current(context) { return context.selection === this.conversation.selection; }

  saveDraft() {
    if (!this.key) return;
    if (this.input.value) localStorage.setItem(draftStorageKey(this.key), this.input.value);
    else localStorage.removeItem(draftStorageKey(this.key));
  }

  async submitting(operation) {
    const context = this.context();
    this.state.sending = true;
    this.onChange();
    try { return await operation(context); }
    finally { this.state.sending = false; this.onChange(); }
  }

  async committed(context, result, { reload = false } = {}) {
    this.onCommit(context);
    if (!this.current(context)) return;
    if (reload) {
      this.state.tokenUsage = null;
      try { await this.conversation.refresh({ reload: true, clear: true }); }
      catch (error) { if (this.current(context)) this.conversation.onError(error); }
      return;
    }
    const turnId = result.turnId || result.turn?.id;
    if (turnId) {
      let turn = this.conversation.turns.find(turn => turn.id === turnId);
      if (!turn) {
        turn = { id: turnId, status: 'inProgress', items: [] };
        this.conversation.turns.push(turn);
      }
      if (this.thread()) this.thread().status = { type: isTurnRunning(turn) ? 'active' : 'idle' };
    }
    this.onChange(true);
  }

  async send({ input = [] } = {}) {
    const state = this.state;
    if (!this.key || state.sending || state.uploadingFiles || state.editing?.saving || this.conversation.loading) return;
    const draft = this.input.value;
    const text = draft.trim();
    const attachments = state.attachments.get(this.key) || [];
    const skills = state.skillInputs.get(this.key) || [];
    if (!text && !attachments.length && !skills.length && !input.length) return;
    const body = { text, actor: 'human' };
    if (attachments.length || skills.length || input.length) body.input = [
      ...(text ? [{ type: 'text', text }] : []), ...input,
      ...attachments.map(file => file.input), ...skills.map(skill => ({ type: 'skill', name: skill.name, path: skill.path })),
    ];
    const clientId = [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
    body.clientUserMessageId = clientId;
    const submission = {
      ...this.context(), body,
      turnId: this.conversation.turns.findLast(isTurnRunning)?.id,
      item: { id: clientId, type: 'userMessage', content: body.input || [{ type: 'text', text }], startedAtMs: Date.now(), _local: true },
    };
    this.conversation.submissions.set(clientId, submission);
    localStorage.removeItem(draftStorageKey(submission.key));
    state.attachments.delete(submission.key);
    state.skillInputs.delete(submission.key);
    this.input.value = '';
    return this.sendSubmission(submission);
  }

  async sendSubmission(submission) {
    if (this.state.sending || this.state.editing?.saving || this.conversation.loading) return;
    Object.assign(submission, this.context());
    return this.submitting(async () => {
      const { body } = submission;
      this.onChange(true);
      try {
        if (submission.archived) await api(`${submission.path}/unarchive`, { method: 'POST', body: {} });
        const result = await api(`${submission.path}/messages`, { method: 'POST', body });
        submission.turnId = result.turnId || result.turn?.id;
        submission.item = { ...submission.item, _sendError: null };
        await this.committed(submission, result);
        return result;
      } catch (error) {
        submission.item = { ...submission.item, _sendError: error.message };
        throw error;
      }
    });
  }

  async interrupt() {
    const turn = this.conversation.turns.findLast(isTurnRunning);
    return api(`${this.conversation.path}/interrupt`, { method: 'POST', body: turn ? { turnId: turn.id } : {} });
  }

  async resume() {
    const turn = this.conversation.turns.at(-1);
    if (!this.key || this.state.sending || this.state.editing?.saving || this.conversation.loading || turn?.status !== 'interrupted') return;
    return this.submitting(async context => {
      const result = await api(`${context.path}/resume`, { method: 'POST', body: { turnId: turn.id } });
      await this.committed(context, result);
    });
  }

  editMessage(itemId, turnId, { regenerate = false, wholeTurn = false } = {}) {
    const state = this.state;
    if (state.sending || state.editing?.saving || (regenerate && state.editing)) return;
    const turn = this.conversation.turns.find(turn => turn.id === turnId);
    const item = turn?.items.find(item => item.id === itemId && item.type === 'userMessage');
    if (!item) return;
    state.editing = { itemId, turnId, text: itemText(item), hasAttachments: item.content.some(part => part.type !== 'text'), saving: false, regenerating: regenerate, wholeTurn };
    const delivery = messageDelivery(item);
    if (delivery) state.editing.origin = { actor: messageOrigin(item).kind, actorName: messageOrigin(item).name, delivery };
    if (regenerate) return this.saveEditedMessage();
    this.onChange();
    return state.editing;
  }

  cancelEdit() {
    if (this.state.editing?.saving) return;
    this.state.editing = null;
    this.onChange();
  }

  async saveEditedMessage() {
    const state = this.state;
    const editing = state.editing;
    if (!editing || editing.saving || state.sending || (!editing.text.trim() && !editing.hasAttachments)) return;
    const context = this.context();
    let path = editing.wholeTurn ? `${context.path}/turns/${encodeURIComponent(editing.turnId)}/regenerate` : `${context.path}/messages/${encodeURIComponent(editing.itemId)}/edit`;
    const text = (editing.origin?.delivery.promptPrefix || '') + editing.text;
    const body = { turnId: editing.turnId, text, actor: 'human', ...editing.origin };
    if (editing.recovery) {
      if (this.running) return;
      path = `${context.path}/messages`;
      body.input = [...editing.recovery.inputPrefix, ...(text.trim() ? [{ type: 'text', text }] : []), ...editing.recovery.attachments];
    }
    editing.saving = true;
    this.onChange();
    try {
      await api(path, { method: 'POST', body, timeout: editing.wholeTurn ? 60_000 : 30_000 });
      if (this.current(context) && state.editing === editing) state.editing = null;
      await this.committed(context, {}, { reload: true });
    } catch (error) {
      editing.regenerating = false;
      editing.wholeTurn = false;
      editing.error = error.message;
      if (error.detail?.data?.reverted) editing.recovery = error.detail.data;
    } finally {
      editing.saving = false;
      if (this.current(context)) this.onChange();
    }
  }

  regenerateTurn(turnId) {
    const turn = this.conversation.turns.find(turn => turn.id === turnId);
    const item = turn?.items.findLast(item => item.type === 'userMessage');
    if (item) return this.editMessage(item.id, turnId, { regenerate: true, wholeTurn: true });
    return this.regenerateHistory(`/turns/${encodeURIComponent(turnId)}/regenerate`, {});
  }

  regenerateMessage(itemId, turnId) {
    return this.regenerateHistory(`/messages/${encodeURIComponent(itemId)}/regenerate`, { turnId });
  }

  async regenerateHistory(path, body) {
    if (!this.key || this.state.sending || this.state.editing) return;
    return this.submitting(async context => {
      const result = await api(`${context.path}${path}`, { method: 'POST', body, timeout: 60_000 });
      await this.committed(context, result, { reload: true });
    });
  }

  async patchSettings(settings) {
    const context = this.context();
    const saveDefaults = Object.keys(settings).filter(key => ['model', 'effort', 'serviceTier'].includes(key));
    this.state.settingsSaving = true;
    this.onChange();
    try {
      const result = await api(`${context.path}/settings`, { method: 'PATCH', body: { ...settings, saveDefaults } });
      if (this.current(context)) this.onSettings(result);
    } finally { this.state.settingsSaving = false; this.onChange(); }
  }

  async addFiles(files, kind = 'file') {
    if (!this.key || this.state.sending || !files.length) return;
    const context = this.context();
    const uploadId = [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
    const directory = `${this.thread().cwd.replace(/\/$/, '')}/.nexus/attachments/${uploadId}`;
    this.state.uploadingFiles += 1;
    this.onChange();
    try {
      const result = await uploadFiles(context.selection.machineId, directory, [...files]);
      const attachments = this.state.attachments.get(context.key) || [];
      attachments.push(...result.files.map(file => fileAttachment(context.selection.machineId, file, kind)));
      this.state.attachments.set(context.key, attachments);
    } finally { this.state.uploadingFiles -= 1; this.onChange(); }
  }

  async answer(form) {
    const itemId = form.dataset.questionItem;
    if (form.hasAttribute('data-submitting') || form.hasAttribute('data-submitted') || (itemId && (this.state.sending || this.state.editing?.saving))) return;
    const questions = [...form.querySelectorAll('[data-question-id]')].map(question => {
      const input = question.querySelector('[data-free-answer]');
      input.setCustomValidity(input.required && !input.value.trim() ? t("请输入回答") : '');
      const answers = [...question.querySelectorAll('input:checked:not([data-other-answer])')].map(input => input.value);
      if (!input.hidden) answers.push(input.value.trim());
      return { id: question.dataset.questionId, title: question.querySelector('legend').textContent, answers };
    });
    if (!form.reportValidity()) return;
    const context = this.context();
    const path = itemId ? `${context.path}/messages` : `/requests/${encodeURIComponent(form.dataset.requestId)}/respond`;
    const body = itemId ? { actor: 'human', text: questions.map(question => `${question.title}\n${question.answers.join('\n')}`).join('\n\n') }
      : { result: { answers: Object.fromEntries(questions.map(question => [question.id, { answers: question.answers }])) } };
    form.setAttribute('data-submitting', '');
    if (itemId) this.state.sending = true;
    setQuestionBusy(form, true);
    this.onChange();
    try {
      await api(path, { method: 'POST', body });
      form.setAttribute('data-submitted', '');
      if (itemId) this.onCommit(context);
      else await this.refreshRequests();
      if (this.current(context)) this.conversation.scheduleRefresh();
    } finally {
      form.removeAttribute('data-submitting');
      if (itemId) this.state.sending = false;
      setQuestionBusy(form, false);
      this.onChange();
    }
  }

  async respond(button) {
    const card = button?.closest('[data-request-id]');
    if (!card) return;
    let result;
    if (button.dataset.requestResult) result = JSON.parse(button.dataset.requestResult);
    else if (button.hasAttribute('data-answer-elicitation')) {
      const raw = card.querySelector('[data-elicitation-raw]')?.value.trim();
      const content = raw ? JSON.parse(raw) : {};
      if (!raw) card.querySelectorAll('[data-elicitation-key]').forEach(input => { content[input.dataset.elicitationKey] = input.dataset.elicitationType === 'json' ? JSON.parse(input.value) : input.type === 'number' ? Number(input.value) : input.value; });
      result = { action: 'accept', content };
    } else if (button.hasAttribute('data-answer-json')) result = JSON.parse(card.querySelector('[data-json-result]').value);
    else return;
    const selection = this.conversation.selection;
    button.disabled = true;
    try {
      await api(`/requests/${encodeURIComponent(card.dataset.requestId)}/respond`, { method: 'POST', body: { result } });
      await this.refreshRequests();
      if (selection === this.conversation.selection) this.conversation.scheduleRefresh();
    } finally { button.disabled = false; }
  }

  bindEditor() {
    this.transcript.addEventListener('click', event => {
      const retry = event.target.closest('[data-retry-message]');
      if (!retry) return;
      const submission = this.conversation.submissions.get(retry.dataset.retryMessage);
      this.sendSubmission(submission).catch(error => {
        if (this.key === submission.key) this.conversation.onError(error);
      });
    });
    this.transcript.addEventListener('input', event => {
      const editing = this.state.editing;
      if (!event.target.matches('.message-edit-input') || !editing) return;
      editing.text = event.target.value;
      editing.error = '';
      event.target.form.querySelector('[type="submit"]').disabled = editing.saving || (!editing.text.trim() && !editing.hasAttachments) || (editing.recovery && this.running);
    });
    this.transcript.addEventListener('compositionstart', event => {
      if (event.target.matches('.message-edit-input') && this.state.editing) this.state.editing.composing = true;
    });
    this.transcript.addEventListener('compositionend', event => {
      if (event.target.matches('.message-edit-input') && this.state.editing) { this.state.editing.composing = false; this.onChange(); }
    });
    this.transcript.addEventListener('keydown', event => {
      if (!event.target.matches('.message-edit-input') || event.isComposing || event.keyCode === 229) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.cancelEdit(); }
    });
  }
}
