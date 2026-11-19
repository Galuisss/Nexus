import { api } from './api.js?v=145';
import { t, locale } from './i18n.js?v=145';
import { h, formatDuration } from './views.js?v=145';
import { icon } from './icons.js?v=145';

const statuses = () => ({
  active: t("进行中"), paused: t("已暂停"), blocked: t("受阻"),
  usageLimited: t("额度受限"), budgetLimited: t("预算已用尽"), complete: t("已完成"),
});

export class GoalPanel {
  constructor({ element, button, conversation, openModal, closeModal }) {
    Object.assign(this, { element, button, conversation, openModal, closeModal });
    element.innerHTML = `<div class="goal-heading"><button type="button" class="goal-disclosure" aria-expanded="false"><span class="goal-label">${icon('target')}Goal</span><span class="goal-status"></span>${icon('chevron-down', 'goal-chevron')}</button><span class="goal-usage"></span><div class="goal-actions"><button type="button" class="goal-primary"></button><details class="goal-menu"><summary class="icon-button">${icon('ellipsis')}</summary><div class="menu-popup"><button type="button" class="goal-toggle"></button><button type="button" class="goal-edit">${icon('pencil')}<span></span></button><button type="button" class="goal-clear">${icon('trash')}<span></span></button></div></details></div></div><button type="button" class="goal-objective" aria-expanded="false"></button><div class="goal-details" hidden><span class="goal-usage-expanded"></span></div><div class="goal-error form-error" role="alert" hidden></div>`;
    this.node = selector => element.querySelector(selector);
    for (const selector of ['.goal-objective', '.goal-disclosure']) this.node(selector).addEventListener('click', () => this.expand(!element.classList.contains('expanded')));
    button.addEventListener('click', () => this.edit());
    this.node('.goal-edit').addEventListener('click', () => { this.node('.goal-menu').open = false; this.edit(); });
    document.addEventListener('click', event => {
      if (!this.node('.goal-menu').contains(event.target)) this.node('.goal-menu').open = false;
    });
    this.node('.goal-menu').addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      this.node('.goal-menu').open = false;
      this.node('.goal-menu summary').focus();
    });
    for (const [selector, command] of [['.goal-primary', 'primary'], ['.goal-toggle', 'toggle'], ['.goal-clear', 'clear']]) {
      this.node(selector).addEventListener('click', () => {
        const selection = this.conversation.selection;
        this.node('.goal-menu').open = false;
        this.command(command).catch(error => {
          if (selection !== this.conversation.selection) return;
          this.error = error.message;
          this.render(this.options);
        });
      });
    }
  }

  expand(expanded) {
    this.element.classList.toggle('expanded', expanded);
    for (const selector of ['.goal-objective', '.goal-disclosure']) this.node(selector).setAttribute('aria-expanded', String(expanded));
    this.node('.goal-disclosure').setAttribute('aria-label', expanded ? t("收起 Goal") : t("展开 Goal"));
    this.node('.goal-details').hidden = !expanded;
  }

  render(options = {}) {
    this.options = options;
    const { running = false, disabled = false } = options;
    const { selection, goal, goalSupported } = this.conversation;
    if (selection !== this.selection) {
      this.selection = selection;
      this.error = '';
      this.expand(false);
      this.node('.goal-menu').open = false;
    }
    const busy = this.busy === selection;
    this.button.hidden = !goalSupported;
    this.button.disabled = disabled || !selection || busy;
    this.button.classList.toggle('enabled', Boolean(goal));
    this.button.setAttribute('aria-label', goal ? t("编辑 Goal") : t("设置 Goal"));
    this.element.hidden = !goal || !goalSupported;
    if (!goal) return;
    this.element.setAttribute('aria-busy', String(busy));
    this.node('.goal-status').textContent = statuses()[goal.status] || goal.status;
    this.node('.goal-status').dataset.status = goal.status;
    this.node('.goal-status').title = statuses()[goal.status] || goal.status;
    const number = value => Number(value || 0).toLocaleString(locale());
    const tokens = goal.tokenBudget == null ? number(goal.tokensUsed) : `${number(goal.tokensUsed)} / ${number(goal.tokenBudget)}`;
    const usage = `${tokens} tokens · ${formatDuration(goal.timeUsedSeconds * 1000)}`;
    this.node('.goal-usage').textContent = usage;
    this.node('.goal-usage-expanded').textContent = usage;
    this.node('.goal-objective').textContent = goal.objective;
    const active = goal.status === 'active';
    const primaryIcon = running ? 'square' : active ? 'pause' : 'play';
    const primaryLabel = running ? t("停止") : active ? t("暂停 Goal") : t("恢复 Goal");
    const compactLabel = running ? t("停止") : active ? t("暂停") : t("恢复");
    this.node('.goal-primary').innerHTML = `${icon(primaryIcon)}<span class="goal-action-full">${primaryLabel}</span><span class="goal-action-compact">${compactLabel}</span>`;
    this.node('.goal-primary').setAttribute('aria-label', primaryLabel);
    this.node('.goal-menu summary').setAttribute('aria-label', t("Goal 操作"));
    this.node('.goal-disclosure').setAttribute('aria-label', this.element.classList.contains('expanded') ? t("收起 Goal") : t("展开 Goal"));
    this.node('.goal-toggle').innerHTML = `${icon(active ? 'pause' : 'play')}<span>${active ? t("暂停 Goal") : t("恢复 Goal")}</span>`;
    this.node('.goal-toggle').hidden = !running;
    for (const [selector, label] of [['.goal-edit', t("编辑 Goal")], ['.goal-clear', t("清除 Goal")]]) {
      this.node(selector).setAttribute('aria-label', label);
      this.node(`${selector} span`).textContent = label;
    }
    for (const button of this.element.querySelectorAll('.goal-actions button')) button.disabled = disabled || busy;
    this.node('.goal-error').hidden = !this.error;
    this.node('.goal-error').textContent = this.error || '';
  }

  async mutate(suffix, method, body) {
    const selection = this.conversation.selection;
    if (!selection || this.busy === selection) return;
    const path = this.conversation.path;
    this.busy = selection;
    this.render(this.options);
    try {
      const result = await api(`${path}/goal${suffix}`, { method, body });
      if (selection !== this.conversation.selection) return result;
      this.conversation.goal = method === 'DELETE' ? null : result.goal;
      this.error = '';
      await this.conversation.refresh({ reload: true });
      return result;
    } finally {
      if (this.busy === selection) this.busy = null;
      this.render(this.options);
    }
  }

  command(args = '') {
    const command = args.trim();
    if (!command) return this.edit();
    if (command === 'primary') return this.command(this.options?.running ? 'stop' : 'toggle');
    if (command === 'toggle') return this.command(this.conversation.goal?.status === 'active' ? 'pause' : 'resume');
    if (command === 'resume') return this.mutate('/resume', 'POST', {});
    if (command === 'stop') return this.mutate('/stop', 'POST', {});
    if (command === 'pause') return this.mutate('', 'PUT', { status: 'paused' });
    if (command === 'clear') return this.mutate('', 'DELETE');
    return this.mutate('', 'PUT', { objective: command, status: 'active' });
  }

  edit() {
    const selection = this.conversation.selection;
    if (!selection) return;
    const goal = this.conversation.goal;
    this.openModal({
      title: goal ? t("编辑 Goal") : t("设置 Goal"),
      submitLabel: goal ? t("保存") : t("启动 Goal"),
      body: `<label class="field"><span>${t("目标")}</span><textarea name="objective" class="goal-editor" rows="6" required>${h(goal?.objective || '')}</textarea></label><div class="field-row goal-form-grid"><label class="field"><span>${t("Token 预算")}</span><input name="tokenBudget" type="number" min="1" step="1" placeholder="${t("不限")}" value="${h(goal?.tokenBudget ?? '')}"></label><label class="field"><span>${t("状态")}</span><select name="status">${Object.entries(statuses()).filter(([value]) => ['active', 'paused', 'complete', goal?.status].includes(value)).map(([value, label]) => `<option value="${value}" ${value === (goal?.status || 'active') ? 'selected' : ''}>${h(label)}</option>`).join('')}</select></label></div>`,
      onSubmit: async form => {
        if (selection !== this.conversation.selection) return this.closeModal();
        const objective = form.get('objective').trim();
        const budget = form.get('tokenBudget').trim();
        await this.mutate('', 'PUT', { objective, status: form.get('status'), tokenBudget: budget ? Number(budget) : null });
        this.closeModal();
      },
    });
  }
}
