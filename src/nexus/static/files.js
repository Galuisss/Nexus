import { t } from './i18n.js?v=145';
import { api, query } from './api.js?v=145';

export const fileUrl = (machineId, path, preview = false) => `/api/machines/${encodeURIComponent(machineId)}/files/download${query({ path, ...(preview ? { preview: true } : {}) })}`;

export function localFileReference(reference) {
  reference = reference.replace(/^([^?#]*[/.][^?#]*?):\d+(?::\d+)?(?=$|[?#])/, '$1');
  return reference && !/^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i.test(reference) ? reference : null;
}

export function localFilePath(base, reference) {
  reference = localFileReference(reference);
  if (!reference) return null;
  const encoded = base.split('/').map(encodeURIComponent).join('/');
  return decodeURIComponent(new URL(reference, `file://${encoded}`).pathname);
}

export const filePreviewUrl = (machineId, file) => `#files${query({ machineId, path: file.slice(0, file.lastIndexOf('/')) || '/', file })}`;

export async function uploadFiles(machineId, directory, files) {
  const body = new FormData();
  body.append('directory', directory);
  for (const file of files) body.append('files', file, file.name || t("图片.png"));
  return api(`/machines/${encodeURIComponent(machineId)}/files`, { method: 'POST', body, timeout: 600_000 });
}

export function fileAttachment(machineId, file, kind = 'file') {
  const image = kind === 'image';
  return {
    name: file.name || file.path.split('/').pop(), path: file.path,
    ...(image ? { preview: fileUrl(machineId, file.path, true) } : {}),
    input: image ? { type: 'localImage', path: file.path } : { type: 'text', text: `Attached file: ${file.path}` },
  };
}
