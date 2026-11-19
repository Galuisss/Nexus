import { t, locale } from './i18n.js?v=145';
import { h } from './views.js?v=145';

export function submitOnEnter(event) {
  if (event.defaultPrevented || event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  const input = event.target;
  if (!input.matches('textarea, input[data-free-answer], input[data-elicitation-key]')) return;
  const button = input.form?.querySelector('button[type="submit"]')
    || input.closest('[data-request-id]')?.querySelector('[data-answer-elicitation], [data-answer-json]');
  if (!button) return;
  event.preventDefault();
  button.click();
}

export function modelId(model) {
  return model.model || model.id;
}

function allEfforts(models, currentModel) {
  const model = models.find(item => modelId(item) === currentModel || item.id === currentModel);
  return (model?.supportedReasoningEfforts || []).map(item => typeof item === 'string'
    ? { value: item, description: '' }
    : { value: item.reasoningEffort || item.effort, description: item.description || '' });
}

export const reasoningEfforts = (models, currentModel) => allEfforts(models, currentModel).filter(item => item.value !== 'ultra');
export const supportsUltra = (models, currentModel) => allEfforts(models, currentModel).some(item => item.value === 'ultra');
export const supportedEffort = (models, currentModel, effort) => allEfforts(models, currentModel).find(item => item.value === effort)?.value;

export function standardEffort(models, currentModel, preferred) {
  const efforts = reasoningEfforts(models, currentModel);
  if (efforts.some(item => item.value === preferred)) return preferred;
  const model = models.find(item => modelId(item) === currentModel || item.id === currentModel);
  return efforts.find(item => item.value === model?.defaultReasoningEffort)?.value || efforts[0]?.value || '';
}

export function ultraField(id, checked = false) {
  return `<label class="switch-field"><span><strong>Ultra</strong><small data-i18n="使用模型预设强度，主动分派子代理">${t("使用模型预设强度，主动分派子代理")}</small></span><input type="checkbox" name="ultra" id="${id}" ${checked ? 'checked' : ''}><span class="switch-track" aria-hidden="true"></span></label>`;
}

export function fastTier(models, currentModel) {
  const model = models.find(item => modelId(item) === currentModel || item.id === currentModel);
  const tier = model?.serviceTiers?.find(tier => (tier.id || tier) === 'priority' || (tier.id || tier) === 'fast' || tier.name?.toLowerCase() === 'fast');
  if (tier) return typeof tier === 'string' ? { id: tier, name: 'Fast' } : tier;
  if (model?.additionalSpeedTiers?.includes('fast')) return { id: 'priority', name: 'Fast' };
  return null;
}

export function speedTiers(models, currentModel) {
  const model = models.find(item => modelId(item) === currentModel || item.id === currentModel);
  const fast = fastTier(models, currentModel);
  const ultrafast = model?.serviceTiers?.find(tier => (tier.id || tier) === 'ultrafast');
  return [
    { id: 'default', name: t("普通") },
    ...(fast ? [fast] : []),
    ...(ultrafast || model?.additionalSpeedTiers?.includes('ultrafast')
      ? [typeof ultrafast === 'object' ? ultrafast : { id: 'ultrafast', name: 'Ultrafast' }] : []),
  ];
}

export function nextSpeedTier(models, model, current) {
  const tiers = speedTiers(models, model);
  const index = tiers.findIndex(tier => tier.id === (current === 'fast' ? fastTier(models, model)?.id : current));
  return tiers[(Math.max(index, 0) + 1) % tiers.length].id;
}

export function renderSpeedControl(button, models, model, current) {
  const tiers = speedTiers(models, model);
  const tier = tiers.find(tier => tier.id === (current === 'fast' ? fastTier(models, model)?.id : current)) || tiers[0];
  const enabled = tier.id !== 'default';
  const next = tiers.find(tier => tier.id === nextSpeedTier(models, model, current));
  button.hidden = tiers.length < 2;
  const label = button.querySelector('.speed-label');
  const text = enabled ? tier.name : 'Fast';
  if (label.textContent !== text) label.textContent = text;
  button.classList.toggle('enabled', enabled);
  button.classList.toggle('ultrafast', tier.id === 'ultrafast');
  button.setAttribute('aria-pressed', String(enabled));
  button.setAttribute('aria-label', t("速度：{0}，切换到 {1}", tier.name, next.name));
}

export const effortLabel = value => ({ none: 'None', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max', ultra: 'Ultra', persistent: 'Persistent' }[value] || value);

export function modelOptions(models, currentModel, placeholder = t("模型")) {
  const options = models.map(model => ({ value: modelId(model), label: model.displayName || modelId(model) }));
  if (currentModel && !options.some(option => option.value === currentModel)) options.unshift({ value: currentModel, label: currentModel });
  return `${currentModel ? '' : `<option value="">${h(placeholder)}</option>`}${options.map(option => `<option value="${h(option.value)}" ${option.value === currentModel ? 'selected' : ''}>${h(option.label)}</option>`).join('')}`;
}

export function effortOptions(models, currentModel, currentEffort, { includeDefault = false, defaultEffort } = {}) {
  if (currentEffort === 'ultra') return '<option value="">Auto</option>';
  const efforts = reasoningEfforts(models, currentModel);
  if (currentEffort && !efforts.some(item => item.value === currentEffort)) efforts.unshift({ value: currentEffort, description: '' });
  const defaultLabel = defaultEffort ? t("{0} · 默认", effortLabel(defaultEffort)) : t("默认");
  const placeholder = includeDefault ? defaultLabel : currentEffort ? '' : t("推理强度");
  return `${placeholder ? `<option value="">${h(placeholder)}</option>` : ''}${efforts.map(item => `<option value="${h(item.value)}" data-tooltip="${h(item.description)}" ${item.value === currentEffort ? 'selected' : ''}>${h(effortLabel(item.value))}</option>`).join('')}`;
}

export function contextInfo(usage) {
  const window = usage?.modelContextWindow;
  const tokens = usage?.last?.totalTokens;
  if (!window || tokens == null) return null;
  return { percent: Math.round(Math.min(1, Math.max(0, tokens / window)) * 100), window, tokens };
}

const number = value => value == null ? '—' : Number(value).toLocaleString(locale());

export function contextContents(usage, compactions = null) {
  const info = contextInfo(usage);
  const last = usage?.last;
  const total = usage?.total;
  return `<div class="context-popover-heading"><span data-i18n="上下文窗口">${t("上下文窗口")}</span><span>${info ? t("{0}% 已使用", info.percent) : '—'}</span></div><dl class="context-stats"><dt data-i18n="当前上下文">${t("当前上下文")}</dt><dd>${number(last?.totalTokens)}</dd><dt data-i18n="窗口上限">${t("窗口上限")}</dt><dd>${number(usage?.modelContextWindow)}</dd>${last ? `<dt data-i18n="输入">${t("输入")}</dt><dd>${number(last.inputTokens)}</dd><dt data-i18n="缓存输入">${t("缓存输入")}</dt><dd>${number(last.cachedInputTokens)}</dd><dt data-i18n="输出">${t("输出")}</dt><dd>${number(last.outputTokens)}</dd><dt data-i18n="推理输出">${t("推理输出")}</dt><dd>${number(last.reasoningOutputTokens)}</dd>` : ''}${compactions === null ? '' : `<dt data-i18n="压缩次数">${t("压缩次数")}</dt><dd>${number(compactions)}</dd>`}${total ? `<dt class="context-total" data-i18n="会话累计">${t("会话累计")}</dt><dd class="context-total">${number(total.totalTokens)}</dd>` : ''}</dl>`;
}

export const slashCommands = () => [
  { name: 'goal', label: t("管理 Goal"), icon: 'target' },
  { name: 'model', label: t("切换模型"), icon: 'sliders' },
  { name: 'effort', label: t("调整推理强度"), icon: 'brain' },
  { name: 'fast', label: t("切换速度"), icon: 'zap' },
  { name: 'ultra', label: t("切换 Ultra"), icon: 'sparkles' },
  { name: 'permissions', label: t("调整会话权限"), icon: 'sliders' },
  { name: 'status', label: t("查看会话与上下文"), icon: 'circle-help' },
  { name: 'new', label: t("创建新会话"), icon: 'square-pen' },
  { name: 'fork', label: t("从最新位置分叉"), icon: 'git-fork' },
  { name: 'side', label: t("打开侧聊"), icon: 'panel-right' },
  { name: 'btw', label: t("打开侧聊"), icon: 'panel-right' },
  { name: 'rename', label: t("重命名会话"), icon: 'pencil' },
  { name: 'archive', label: t("归档当前会话"), icon: 'archive' },
  { name: 'compact', label: t("整理上下文"), icon: 'sparkles' },
  { name: 'review', label: t("审查当前工作区改动"), icon: 'file-code' },
  { name: 'plan', label: t("切换计划模式"), icon: 'file-code' },
  { name: 'diff', label: t("查看工作区改动"), icon: 'file-code' },
  { name: 'ps', label: t("管理后台任务"), icon: 'terminal' },
  { name: 'skills', label: t("选择技能"), icon: 'sparkles' },
  { name: 'mcp', label: t("查看 MCP 工具"), icon: 'server' },
];
