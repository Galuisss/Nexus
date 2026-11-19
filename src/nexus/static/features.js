import { api } from './api.js?v=145';
import { setSettingsDisabled } from './select.js?v=145';

export function featuresPanel(features = {}) {
  return `<label class="switch-field"><span><strong>Tool call ID</strong></span><input type="checkbox" role="switch" id="feature-tool-call-id" aria-label="Tool call ID" ${features.toolCallId ? 'checked' : ''}><span class="switch-track" aria-hidden="true"></span></label>`;
}

export function bindFeatures(config) {
  const control = document.getElementById('feature-tool-call-id');
  const error = document.getElementById('modal-error');
  let saving = false;
  let touched = false;
  const showError = message => {
    if (!control.isConnected) return;
    error.textContent = message;
    error.hidden = !message;
  };
  control.onchange = async () => {
    touched = true;
    showError('');
    saving = true;
    control.closest('label').dataset.saving = '';
    control.disabled = true;
    try {
      config.features = await api('/features', { method: 'PATCH', body: { toolCallId: control.checked } });
    } catch (failure) {
      control.checked = Boolean(config.features?.toolCallId);
      showError(failure.message);
    } finally {
      saving = false;
      control.disabled = false;
      delete control.closest('label').dataset.saving;
    }
  };
  api('/features').then(features => {
    if (touched) return;
    config.features = features;
    if (control.isConnected && !saving) control.checked = features.toolCallId;
  }).catch(failure => { if (!touched) showError(failure.message); });
}

export function toolCompactControl(control, settings, unavailable, saving) {
  control.hidden = !settings.toolCallId;
  control.classList.toggle('enabled', Boolean(settings.toolCallCompact));
  control.setAttribute('aria-pressed', String(Boolean(settings.toolCallCompact)));
  setSettingsDisabled(control, unavailable, saving);
}
