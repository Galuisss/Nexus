import { api } from './api.js?v=145';
import { t, locale } from './i18n.js?v=145';
import { icon } from './icons.js?v=145';
import { h } from './views.js?v=145';

function timestamp(seconds) {
  if (seconds == null) return '—';
  const date = new Date(seconds * 1000);
  const text = new Intl.DateTimeFormat(locale(), { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
  return `<time datetime="${date.toISOString()}" title="${h(date.toLocaleString(locale(), { timeZoneName: 'short' }))}">${h(text)}</time>`;
}

function machines(items) {
  return `<div class="account-machines">${items.map(machine => `<span class="account-machine" title="${h(t(machine.error || ({ connected: '已连接', disconnected: '未连接', connecting: '连接中', reconnecting: '正在重连' }[machine.status] || '连接出错')))}"><i class="status-dot ${h(machine.status)}"></i>${h(machine.name)}</span>`).join('')}</div>`;
}

function windowCard(label, window) {
  const remaining = window ? Math.max(0, Math.min(100, 100 - window.usedPercent)) : null;
  const color = remaining === null ? 'unknown' : remaining <= 10 ? 'critical' : remaining <= 25 ? 'low' : '';
  return `<div class="quota-window ${color}"><div class="quota-window-heading"><span>${h(label)}</span><strong>${remaining === null ? '—' : h(t('剩余 {0}%', remaining))}</strong></div><div class="quota-track" ${remaining === null ? '' : `role="progressbar" aria-label="${h(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${remaining}" aria-valuetext="${h(t('剩余 {0}%', remaining))}"`}><span style="width:${remaining ?? 0}%"></span></div><div class="quota-reset"><span>${t('刷新时间')}</span>${timestamp(window?.resetsAt)}</div></div>`;
}

function quotaBucket(bucket) {
  const windows = [bucket.primary, bucket.secondary].filter(Boolean);
  const name = bucket.limitName || bucket.normalModelSlug || (bucket.limitId !== 'codex' ? bucket.limitId : '');
  const extras = windows.filter(window => window.windowDurationMins !== 300 && window.windowDurationMins !== 10080);
  return `<section class="quota-bucket">${name ? `<h4>${h(name)}</h4>` : ''}<div class="quota-windows">${windowCard(t('五小时限额'), windows.find(window => window.windowDurationMins === 300))}${windowCard(t('周限额'), windows.find(window => window.windowDurationMins === 10080))}${extras.map(window => windowCard(window.windowDurationMins ? t('{0} 分钟限额', window.windowDurationMins) : t('限额'), window)).join('')}</div></section>`;
}

function creditBalance(credits) {
  const balance = credits?.unlimited ? t('不限') : credits?.balance != null
    ? new Intl.NumberFormat(locale(), { maximumFractionDigits: 2 }).format(Number(credits.balance)) : '—';
  return `<div class="account-credit-balance"><span>${t('额度')}</span><strong>${h(balance)}</strong></div>`;
}

function resets(summary) {
  const heading = `<div class="account-reset-count"><span>${t('重置卡')}</span><strong>${summary ? h(t('{0} 张', summary.availableCount)) : '—'}</strong></div>`;
  if (summary?.availableCount === 0) return `<section class="account-resets">${heading}</section>`;
  const expirations = new Map();
  for (const credit of summary?.credits || []) {
    if (credit.status !== 'available') continue;
    const key = credit.expiresAt ?? null;
    expirations.set(key, (expirations.get(key) || 0) + 1);
  }
  const rows = [...expirations].sort(([a], [b]) => (a ?? Infinity) - (b ?? Infinity));
  return `<section class="account-resets">${heading}<dl class="account-expirations">${rows.length ? `<div><dt>${t('到期时间')}</dt><dd></dd></div>${rows.map(([expires, count]) => `<div><dt>${expires === null ? t('长期有效') : timestamp(expires)}</dt><dd>${h(t('{0} 张', count))}</dd></div>`).join('')}` : `<div><dt>${t('到期时间')}</dt><dd>—</dd></div>`}</dl></section>`;
}

function accountCard(account, distinguishWorkspace) {
  const usage = account.usage;
  const buckets = Object.values(usage?.rateLimitsByLimitId || {});
  if (!buckets.length) buckets.push(usage?.rateLimits || {});
  const planType = usage?.rateLimits?.planType || usage?.rateLimitsByLimitId?.codex?.planType || account.planType;
  const plan = ({ prolite: 'Pro 10x', pro: 'Pro 20x', promax: 'Pro 50x' })[planType]
    || (planType ? planType.replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase()) : '');
  const workspace = account.workspaceName || (distinguishWorkspace ? t('工作区 {0}', account.accountId?.slice(0, 8) || account.machines[0].name) : '');
  return `<article class="account-card"><header class="account-card-heading"><h3>${h(account.email || account.userId || t('ChatGPT 账号'))}</h3>${plan ? `<span class="account-plan">${h(plan)}</span>` : ''}</header>${workspace ? `<div class="account-workspace">${h(workspace)}</div>` : ''}${machines(account.machines)}<div class="account-quotas">${buckets.map(quotaBucket).join('')}</div><div class="account-balances">${creditBalance(usage?.rateLimits?.credits || usage?.rateLimitsByLimitId?.codex?.credits)}${resets(usage?.rateLimitResetCredits)}</div>${account.error ? `<div class="account-error" role="alert">${h(t(account.error))}${account.updatedAt ? `<span>${h(t('更新于 {0}', new Date(account.updatedAt).toLocaleString(locale())))}</span>` : ''}</div>` : ''}</article>`;
}

function otherMachine(machine) {
  const labels = { apiKey: 'API Key', amazonBedrock: 'Amazon Bedrock', gcp: 'Google Cloud', external: '自定义接口', loggedOut: '未登录', unavailable: '未连接' };
  const label = machine.authType === 'unavailable' ? ({ connected: '读取账号失败', connecting: '连接中', reconnecting: '正在重连', error: '连接出错' }[machine.status] || '未连接') : labels[machine.authType] || machine.authType;
  return `<article class="account-other"><div class="account-other-heading">${machines([machine])}<span>${h(machine.providerName || t(label))}</span></div>${machine.error ? `<div class="account-error" role="alert">${h(t(machine.error))}</div>` : ''}</article>`;
}

export class AccountLimits {
  constructor(openModal) {
    this.openModal = openModal;
    this.data = null;
    this.error = '';
    this.loading = null;
    this.timer = null;
    this.dirty = false;
    document.getElementById('modal').addEventListener('close', () => clearTimeout(this.timer));
  }

  get root() { return document.getElementById('account-limits'); }
  get visible() { return Boolean(document.getElementById('modal').open && this.root); }

  open() {
    clearTimeout(this.timer);
    this.openModal({
      title: t('账号限额'), footer: false,
      body: `<section id="account-limits"><div class="account-limits-toolbar"><button type="button" class="icon-button" id="refresh-account-limits" aria-label="${t('刷新限额')}" data-tooltip="${t('刷新限额')}">${icon('refresh')}</button></div><div class="account-limits-content"></div><div class="account-limits-error" role="alert"></div></section>`,
      afterRender: () => {
        this.root.querySelector('#refresh-account-limits').addEventListener('click', () => this.refresh(true));
        this.render();
        this.refresh();
      },
    });
  }

  render() {
    if (!this.visible) return;
    document.getElementById('modal-title').textContent = t('账号限额');
    const button = this.root.querySelector('#refresh-account-limits');
    button.setAttribute('aria-label', t('刷新限额'));
    button.dataset.tooltip = t('刷新限额');
    button.setAttribute('aria-busy', String(Boolean(this.loading)));
    button.disabled = Boolean(this.loading);
    this.root.querySelector('.account-limits-error').textContent = t(this.error);
    const rows = this.data ? [
      ...this.data.accounts.map(account => ({ key: account.id, html: accountCard(account, this.data.accounts.some(other => other.id !== account.id && other.email === account.email)) })),
      ...this.data.otherMachines.map(machine => ({ key: `machine:${machine.id}`, html: otherMachine(machine) })),
    ] : [{ key: 'loading', html: `<div class="account-limits-empty" role="status">${this.error ? t('限额暂不可用') : t('正在读取限额…')}</div>` }];
    if (!rows.length) rows.push({ key: 'empty', html: `<div class="account-limits-empty">${t('暂无机器')}</div>` });
    const content = this.root.querySelector('.account-limits-content');
    const previous = new Map([...content.children].map(node => [node.dataset.key, node]));
    rows.forEach(({ key, html }, index) => {
      let node = previous.get(key);
      previous.delete(key);
      if (!node || node.renderedHTML !== html) {
        const template = document.createElement('template');
        template.innerHTML = html;
        const replacement = template.content.firstElementChild;
        replacement.dataset.key = key;
        replacement.renderedHTML = html;
        if (node) node.replaceWith(replacement);
        node = replacement;
      }
      if (content.children[index] !== node) content.insertBefore(node, content.children[index] || null);
    });
    previous.forEach(node => node.remove());
  }

  async refresh(force = false) {
    if (!this.visible || this.loading) return;
    clearTimeout(this.timer);
    this.loading = api(`/accounts/usage${force ? '?refresh=true' : ''}`, { timeout: 65_000 });
    this.render();
    try {
      this.data = await this.loading;
      this.error = '';
    } catch (error) {
      this.error = error.message;
    } finally {
      this.loading = null;
      if (this.visible) {
        this.render();
        this.timer = setTimeout(() => this.refresh(), this.dirty ? 500 : 60_000);
        this.dirty = false;
      }
    }
  }

  handleEvent(event) {
    if (!this.visible || !['account/rateLimits/updated', 'account/updated', 'account/login/completed', 'nexus/machine/updated', 'nexus/resync'].includes(event.method)) return;
    if (this.loading) { this.dirty = true; return; }
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refresh(), 500);
  }
}
