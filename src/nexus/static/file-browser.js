import { t } from './i18n.js?v=145';
import { api, query } from './api.js?v=145';
import { fileUrl, filePreviewUrl, localFilePath } from './files.js?v=145';
import { icon } from './icons.js?v=145';
import { h } from './views.js?v=145';
import { markdown } from './markdown.js?v=145';
import { mountMermaid } from './mermaid.js?v=145';
import { setupMachineFilter } from './machine-filter.js?v=145';
import { showContextMenu } from './context-menu.js?v=145';

const $ = id => document.getElementById(id);
const imagePattern = /\.(png|jpe?g|webp|gif|bmp|svg|ico|avif)$/i;
const markdownPattern = /\.(md|markdown|mdown)$/i;
const maxTextPreviewSize = 2 * 1024 * 1024;
const editorText = text => text.replace(/\r\n?/g, '\n');

export class FileBrowser {
  constructor({ onNavigate, onBack, onOpenNavigation, onOpenTerminal, openModal, closeModal, toast }) {
    this.onNavigate = onNavigate;
    this.toast = toast;
    this.onOpenTerminal = onOpenTerminal;
    this.openModal = openModal;
    this.closeModal = closeModal;
    this.paths = new Map();
    this.histories = new Map();
    this.entries = [];
    this.navigation = 0;
    this.objectUrl = null;
    this.setupContextMenu();
    $('files-open-nav').addEventListener('click', onOpenNavigation);
    this.machineFilter = setupMachineFilter(([machine]) => {
      this.open({ machines: this.machines, machineId: machine.id });
    }, { id: 'files-machine-filter', multiple: false });
    $('files-directory-form').addEventListener('submit', event => {
      event.preventDefault();
      this.browse($('files-directory').value.trim());
    });
    $('files-back').addEventListener('click', () => this.travel(-1));
    $('files-forward').addEventListener('click', () => this.travel(1));
    $('files-parent').addEventListener('click', () => this.browse(this.parent));
    $('files-refresh').addEventListener('click', () => this.loadEntries());
    $('files-search').addEventListener('input', () => {
      $('clear-files-search').hidden = !$('files-search').value;
      clearTimeout(this.searchTimer);
      this.searchTimer = setTimeout(() => this.loadEntries(), 250);
    });
    $('files-search-mode').addEventListener('change', () => {
      clearTimeout(this.searchTimer);
      this.loadEntries();
    });
    $('clear-files-search').addEventListener('click', () => {
      clearTimeout(this.searchTimer);
      $('files-search').value = '';
      $('clear-files-search').hidden = true;
      $('files-search').focus();
      this.loadEntries();
    });
    $('files-list').addEventListener('click', event => {
      if (Date.now() < this.suppressClickUntil) { event.preventDefault(); return; }
      const entry = event.target.closest('[data-file-index]');
      if (!entry) return;
      const file = this.entries[Number(entry.dataset.fileIndex)];
      if (file.isDirectory) this.browse(file.path);
      else this.preview(file);
    });
    $('files-preview-back').addEventListener('click', onBack);
    $('files-preview-mode').addEventListener('click', () => {
      this.source = !this.source;
      this.renderText();
    });
    $('files-edit').addEventListener('click', () => {
      this.editing = true;
      this.draft = editorText(this.text);
      this.source = true;
      this.renderText();
    });
    $('files-save').addEventListener('click', () => this.save());
    $('files-cancel').addEventListener('click', () => {
      if (!this.canLeave()) return;
      this.editing = false;
      this.source = false;
      $('files-edit-error').hidden = true;
      this.renderText();
    });
    $('files-preview-content').addEventListener('input', event => {
      if (!event.target.matches('.files-editor')) return;
      this.draft = event.target.value;
      this.updateEditorControls();
    });
    $('files-preview-content').addEventListener('keydown', event => {
      if (event.key !== 'Tab' || !event.target.matches('.files-editor')) return;
      event.preventDefault();
      event.target.setRangeText('\t', event.target.selectionStart, event.target.selectionEnd, 'end');
      event.target.dispatchEvent(new Event('input', { bubbles: true }));
    });
    document.addEventListener('keydown', event => {
      if (!this.editing || $('files-workspace').hidden || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return;
      event.preventDefault();
      this.save();
    });
    window.addEventListener('beforeunload', event => {
      if (!this.dirty && !this.saving) return;
      event.preventDefault();
      event.returnValue = '';
    });
    $('files-preview-content').addEventListener('click', event => {
      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const anchor = event.target.closest('a[href^="#"]:not([data-file-path])');
      if (anchor) {
        event.preventDefault();
        document.getElementById(anchor.getAttribute('href').slice(1))?.scrollIntoView();
        return;
      }
      const link = event.target.closest('a[data-file-path]');
      if (!link) return;
      event.preventDefault();
      const path = link.dataset.filePath;
      this.openPath(path);
    });
  }

  setupContextMenu() {
    const list = $('files-list');
    const cancel = () => { clearTimeout(this.longPress); this.longPress = null; };
    let closeMenu;
    let touchMenuOpened = false;
    const entryAt = target => {
      const row = target.closest('[data-file-entry]');
      return row && this.entries[Number(row.dataset.fileEntry)];
    };
    const show = (event, x = event.clientX, y = event.clientY) => {
      cancel();
      const { target } = event;
      const file = entryAt(target);
      const { machineId, path } = this;
      const items = [];
      if (file) {
        if (file.isDirectory) items.push(['terminal', t("打开终端"), async () => {
          try { await this.onOpenTerminal(machineId, file.path); }
          catch (error) { this.toast(error.message, true); }
        }]);
        items.push(['pencil', t("重命名"), () => this.rename(file)]);
      } else if (path && this.paths.get(machineId) === path && !this.loading) {
        items.push(
          ['folder', t("新建文件夹"), () => this.create(machineId, path, true)],
          ['file-code', t("新建文件"), () => this.create(machineId, path, false)],
        );
      }
      if (!items.length) return false;
      closeMenu = showContextMenu({ target, x, y }, items);
      return true;
    };
    list.addEventListener('contextmenu', event => {
      event.preventDefault();
      show(event);
    });
    list.addEventListener('pointerdown', event => {
      cancel();
      if (event.pointerType === 'mouse') return;
      touchMenuOpened = false;
      const { clientX, clientY } = event;
      this.pressPosition = { clientX, clientY };
      this.longPress = setTimeout(() => {
        if (!show(event)) return;
        if (event.pointerType === 'touch') touchMenuOpened = true;
        else this.suppressClickUntil = Date.now() + 1000;
      }, 500);
    });
    list.addEventListener('pointermove', event => {
      if (this.longPress && Math.hypot(event.clientX - this.pressPosition.clientX, event.clientY - this.pressPosition.clientY) > 10) cancel();
    });
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) list.addEventListener(type, cancel);
    list.addEventListener('touchend', event => {
      if (touchMenuOpened) event.preventDefault();
      touchMenuOpened = false;
    }, { passive: false });
    list.addEventListener('scroll', () => { cancel(); closeMenu?.(); }, { passive: true });
    list.addEventListener('keydown', event => {
      if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
      event.preventDefault();
      const bounds = event.target.getBoundingClientRect();
      show(event, bounds.left + 20, event.target === list ? bounds.top + 20 : bounds.bottom);
    });
  }

  create(machineId, directory, isDirectory) {
    this.openModal({
      title: isDirectory ? t("新建文件夹") : t("新建文件"), submitLabel: t("新建"),
      body: `<label class="field"><span data-i18n="名称">${t("名称")}</span><input name="name" required autocomplete="off" autofocus></label>`,
      onSubmit: async form => {
        await api(`/machines/${encodeURIComponent(machineId)}/files/create`, {
          method: 'POST', body: { directory, name: form.get('name'), isDirectory },
        });
        if (this.machineId === machineId && this.path === directory) await this.loadEntries();
        this.closeModal();
      },
    });
  }

  rename(file) {
    const machineId = this.machineId;
    this.openModal({
      title: t("重命名"), submitLabel: t("重命名"),
      body: `<label class="field"><span data-i18n="名称">${t("名称")}</span><input name="name" value="${h(file.name)}" required autocomplete="off" autofocus></label>`,
      afterRender: () => {
        const input = document.querySelector('#modal-body input');
        input.focus();
        input.setSelectionRange(0, !file.isDirectory && file.name.lastIndexOf('.') > 0 ? file.name.lastIndexOf('.') : file.name.length);
      },
      onSubmit: async form => {
        const result = await api(`/machines/${encodeURIComponent(machineId)}/files/rename`, {
          method: 'POST', body: { path: file.path, name: form.get('name') },
        });
        const replace = path => path === file.path || path?.startsWith(`${file.path}/`) ? result.path + path.slice(file.path.length) : path;
        const history = this.histories.get(machineId);
        if (history) {
          for (const entry of history.entries) {
            entry.path = replace(entry.path);
            entry.file = replace(entry.file);
          }
        }
        if (this.machineId === machineId && this.file && replace(this.file.path) !== this.file.path) {
          this.file = { ...this.file, path: replace(this.file.path), name: form.get('name') };
          $('files-preview-name').dataset.tooltip = this.file.path;
          $('files-preview-download').href = fileUrl(machineId, this.file.path);
          $('files-preview-download').download = this.file.name;
          $('files-preview-mode').hidden = !this.editable || !markdownPattern.test(this.file.name);
          if (this.editable) this.renderText();
          else this.updateEditorControls();
        }
        if (this.machineId === machineId) await this.loadEntries();
        this.closeModal();
      },
    });
  }

  route() {
    this.onNavigate({ machineId: this.machineId, path: this.path, file: this.file?.path });
  }

  get dirty() {
    return this.editing && this.draft !== editorText(this.text);
  }

  canLeave() {
    if (this.saving) {
      this.toast(t("正在保存文件"));
      return false;
    }
    return !this.dirty || window.confirm(t("放弃当前文件未保存的修改？"));
  }

  updateEditorControls() {
    $('files-edit').hidden = !this.editable || this.editing;
    $('files-save').hidden = !this.editing;
    $('files-cancel').hidden = !this.editing;
    $('files-save').disabled = this.saving || !this.dirty;
    $('files-save').textContent = this.saving ? t("保存中…") : t("保存");
    $('files-cancel').disabled = Boolean(this.saving);
    $('files-preview-mode').disabled = Boolean(this.saving);
    $('files-preview-name').textContent = `${this.file?.name || ''}${this.dirty ? ' *' : ''}`;
  }

  async save() {
    if (!this.dirty || this.saving) return;
    const content = this.text.includes('\r\n') ? this.draft.replace(/\n/g, '\r\n')
      : this.text.includes('\r') && !this.text.includes('\n') ? this.draft.replace(/\n/g, '\r') : this.draft;
    this.saving = true;
    this.updateEditorControls();
    const editor = $('files-preview-content').querySelector('.files-editor');
    if (editor) editor.readOnly = true;
    $('files-edit-error').hidden = true;
    try {
      await api(`/machines/${encodeURIComponent(this.machineId)}/files/text`, {
        method: 'PUT', body: { path: this.file.path, content, originalContent: this.text },
      });
      this.text = content;
      this.toast(t("文件已保存"));
    } catch (error) {
      $('files-edit-error').textContent = error.message;
      $('files-edit-error').hidden = false;
    } finally {
      this.saving = false;
      if (editor) editor.readOnly = false;
      this.updateEditorControls();
    }
  }

  async open({ machines, machineId, path, file }) {
    this.machines = machines;
    machineId = machineId || this.machineId || machines[0]?.id;
    const machine = machines.find(item => item.id === machineId);
    if (!machine) return;
    if (!this.histories.has(machineId)) this.histories.set(machineId, { entries: [], index: -1 });
    const history = this.histories.get(machineId);
    const current = history.entries[history.index];
    const destination = path || file ? { path: path || file.slice(0, file.lastIndexOf('/')) || '/', file: file || null }
      : current || { path: machine.projects?.[0] || '~', file: null };
    if (this.machineId !== machineId) {
      if (!this.canLeave()) {
        this.machineFilter.update(machines, this.machineId);
        this.route();
        return;
      }
      this.rememberPosition();
      this.resetPreview();
      this.machineId = machineId;
      this.path = null;
      this.parent = null;
      this.entries = [];
      $('files-directory').value = destination.path;
    }
    this.machineFilter.update(machines, machineId);
    return this.navigate(destination, { reload: true });
  }

  browse(path) {
    return this.navigate({ path, file: null });
  }

  preview(file) {
    return this.navigate({ path: this.path, file: file.path }, { reload: file.path === this.file?.path });
  }

  openPath(path) {
    return this.navigate({ path: path.slice(0, path.lastIndexOf('/')) || '/', file: path }, { reload: path === this.file?.path });
  }

  loadEntries() {
    return this.navigate({ path: this.path, file: this.file?.path || null }, { reload: true });
  }

  rememberPosition() {
    const history = this.histories.get(this.machineId);
    const entry = history?.entries[history.index];
    if (!entry || entry.path !== this.path || entry.file !== (this.file?.path || null)) return;
    if ($('files-browser').getClientRects().length) {
      entry.listScroll = { top: $('files-list').scrollTop, left: $('files-list').scrollLeft };
    }
    const content = $('files-preview-content').querySelector('.files-editor') || $('files-preview-content');
    entry.previewScroll = { top: content.scrollTop, left: content.scrollLeft };
    entry.source = Boolean(this.source);
  }

  restorePosition(entry, generation) {
    const restore = () => {
      if (generation !== this.navigation) return;
      $('files-list').scrollTo(entry.listScroll || { top: 0, left: 0 });
      const content = $('files-preview-content').querySelector('.files-editor') || $('files-preview-content');
      content.scrollTo(entry.previewScroll || { top: 0, left: 0 });
    };
    restore();
    requestAnimationFrame(restore);
    for (const image of $('files-preview-content').querySelectorAll('img')) {
      image.addEventListener('load', restore, { once: true });
    }
  }

  travel(direction) {
    const history = this.histories.get(this.machineId);
    const index = history.index + direction;
    const entry = history.entries[index];
    if (entry) return this.navigate(entry, { index, direction });
  }

  updateNavigation() {
    const history = this.histories.get(this.machineId);
    $('files-back').disabled = this.loading || !history || history.index <= 0;
    $('files-forward').disabled = this.loading || !history || history.index >= history.entries.length - 1;
    $('files-parent').disabled = this.loading || !this.parent || this.path === this.parent;
  }

  async navigate(destination, { index, direction = 0, reload = false } = {}) {
    clearTimeout(this.searchTimer);
    const generation = ++this.navigation;
    this.abort?.abort();
    const abort = this.abort = new AbortController();
    const machineId = this.machineId;
    const history = this.histories.get(machineId);
    let location = { ...destination };
    this.loading = true;
    this.updateNavigation();
    const draft = $('files-directory').value;
    let preview;
    $('files-refresh').classList.add('refreshing');
    $('files-workspace').setAttribute('aria-busy', 'true');
    try {
      let directory;
      let file;
      while (true) {
        try {
          directory = reload || location.path !== this.path || !this.paths.has(machineId)
            ? await api(`/machines/${encodeURIComponent(machineId)}/files${query({
              path: location.path, searchTerm: $('files-search').value.trim(), mode: $('files-search-mode').value,
            })}`)
            : { path: this.path, parent: this.parent, entries: this.entries };
          if (generation !== this.navigation) return false;
          if (location.file === directory.path || location.file === `${directory.path}/`) location.file = null;
          file = location.file && (directory.entries.find(item => item.path === location.file)
            || { name: location.file.split('/').pop(), path: location.file });
          if (file?.isDirectory) {
            location = { path: file.path, file: null };
            continue;
          }
          location.path = directory.path;
          break;
        } catch (error) {
          if (generation !== this.navigation) return false;
          if (!direction || error.detail?.code !== 'directory_not_found') throw error;
          history.entries.splice(index, 1);
          if (index < history.index) history.index -= 1;
          if (direction < 0) index -= 1;
          if (!history.entries[index]) throw error;
          location = { ...history.entries[index] };
        }
      }
      const replacePreview = this.path !== location.path || (this.file?.path || null) !== location.file || Boolean(direction) || (reload && !this.editing);
      if (file && replacePreview) {
        try { preview = await this.readPreview(machineId, file, abort.signal); }
        catch (error) { preview = { kind: 'error', message: error.message }; }
      }
      if (generation !== this.navigation) return false;
      if (replacePreview && !this.canLeave()) {
        $('files-directory').value = this.path;
        this.route();
        return false;
      }
      this.rememberPosition();
      const current = history.entries[history.index];
      if (direction) {
        history.index = index;
      } else if (current?.path !== location.path || current?.file !== location.file) {
        history.entries.splice(history.index + 1);
        history.entries.push({
          path: location.path, file: location.file,
          listScroll: current?.path === location.path ? current.listScroll : { top: 0, left: 0 },
        });
        history.index = history.entries.length - 1;
      }
      const entry = history.entries[history.index];
      this.path = location.path;
      this.parent = directory.parent;
      this.paths.set(this.machineId, this.path);
      this.entries = directory.entries;
      if (replacePreview) {
        this.resetPreview();
        if (file) this.renderPreview(file, preview, Boolean(entry.source));
      }
      if ($('files-directory').value === draft) $('files-directory').value = this.path;
      $('files-error').hidden = true;
      this.renderEntries();
      this.route();
      this.restorePosition(entry, generation);
      return true;
    } catch (error) {
      if (generation !== this.navigation) return false;
      $('files-error').textContent = error.message;
      $('files-error').hidden = false;
      return false;
    } finally {
      if (preview?.objectUrl && preview.objectUrl !== this.objectUrl) URL.revokeObjectURL(preview.objectUrl);
      if (generation === this.navigation) {
        this.loading = false;
        $('files-refresh').classList.remove('refreshing');
        $('files-workspace').removeAttribute('aria-busy');
        this.updateNavigation();
      }
    }
  }

  renderEntries() {
    const entries = this.entries.map((file, index) => ({ file, index }));
    $('files-list').innerHTML = entries.length ? entries.map(({ file, index }) => `<div data-file-entry="${index}" class="files-entry ${this.file?.path === file.path ? 'selected' : ''}"><button type="button" class="files-entry-open" data-file-index="${index}" data-tooltip="${h(file.path)}">${icon(file.isDirectory ? 'folder' : imagePattern.test(file.name) ? 'image' : 'file-code')}<span>${h(file.name)}</span>${file.isDirectory ? icon('chevron-right') : ''}</button>${file.isDirectory ? '' : `<a class="icon-button small files-entry-download" href="${h(fileUrl(this.machineId, file.path))}" download aria-label="${t("下载 {0}", h(file.name))}" data-tooltip="${t("下载")}" data-i18n-data-tooltip="下载">${icon('download')}</a>`}</div>`).join('') : `<div class="list-empty" data-i18n="暂无文件">${t("暂无文件")}</div>`;
  }

  resetPreview() {
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
    this.objectUrl = null;
    this.file = null;
    this.text = '';
    this.draft = '';
    this.editing = false;
    this.editable = false;
    $('files-workspace').classList.remove('previewing');
    $('files-preview-toolbar').hidden = true;
    $('files-edit-error').hidden = true;
    this.updateEditorControls();
    $('files-preview-content').className = 'files-preview-content';
    $('files-preview-content').innerHTML = `<div class="files-empty">${icon('file-code')}<span data-i18n="选择文件">${t("选择文件")}</span></div>`;
  }

  async readPreview(machineId, file, signal) {
    const isImage = imagePattern.test(file.name);
    const isPdf = /\.pdf$/i.test(file.name);
    if (!isImage && !isPdf && file.size > maxTextPreviewSize) return { kind: 'download', message: t("文件超过 2 MiB") };
    const response = await fetch(fileUrl(machineId, file.path, isImage), { signal, cache: 'no-store' });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error?.message || t("无法读取文件"));
    }
    if (!isImage && !isPdf && Number(response.headers.get('Content-Length')) > maxTextPreviewSize) {
      await response.body.cancel();
      return { kind: 'download', message: t("文件超过 2 MiB") };
    }
    if (isImage) {
      const objectUrl = URL.createObjectURL(await response.blob());
      try {
        const image = new Image();
        image.alt = file.name;
        image.src = objectUrl;
        await image.decode();
        return { kind: 'image', image, objectUrl };
      } catch (error) {
        URL.revokeObjectURL(objectUrl);
        throw error;
      }
    }
    if (isPdf) return { kind: 'pdf', blob: await response.blob() };
    const bytes = await response.arrayBuffer();
    let text;
    try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes); }
    catch { return { kind: 'download' }; }
    return text.includes('\0') ? { kind: 'download' } : { kind: 'text', text };
  }

  renderPreview(file, preview, source) {
    this.file = file;
    this.source = source;
    $('files-workspace').classList.add('previewing');
    $('files-preview-toolbar').hidden = false;
    $('files-preview-name').textContent = file.name;
    $('files-preview-name').dataset.tooltip = file.path;
    $('files-preview-download').href = fileUrl(this.machineId, file.path);
    $('files-preview-download').download = file.name;
    $('files-preview-mode').hidden = true;
    const content = $('files-preview-content');
    content.className = 'files-preview-content';
    if (preview.kind === 'image') {
      this.objectUrl = preview.objectUrl;
      const wrapper = document.createElement('div');
      wrapper.className = 'files-image';
      wrapper.append(preview.image);
      content.replaceChildren(wrapper);
    } else if (preview.kind === 'pdf') {
      this.objectUrl = URL.createObjectURL(new Blob([preview.blob], { type: 'application/pdf' }));
      content.innerHTML = `<iframe class="files-pdf" src="${h(this.objectUrl)}" title="${h(file.name)}"></iframe>`;
    } else if (preview.kind === 'text') {
      this.text = preview.text;
      this.editable = true;
      $('files-preview-mode').hidden = !markdownPattern.test(file.name);
      this.renderText();
    } else if (preview.kind === 'download') {
      this.renderDownload(preview.message);
    } else {
      content.innerHTML = `<div class="files-preview-error" role="alert">${h(preview.message)}</div>`;
    }
  }

  renderDownload(message = this.file.name) {
    $('files-preview-content').innerHTML = `<div class="files-empty">${icon('file-code')}<span>${h(message)}</span><a class="button subtle" href="${h(fileUrl(this.machineId, this.file.path))}" download>${t("{0}下载文件", icon('download'))}</a></div>`;
  }

  renderText() {
    const content = $('files-preview-content');
    const rendered = markdownPattern.test(this.file.name) && !this.source;
    const text = this.editing ? this.draft : this.text;
    $('files-preview-mode').textContent = this.source ? t("预览") : this.editing ? t("编辑") : t("源码");
    $('files-preview-mode').setAttribute('aria-pressed', String(this.source));
    this.updateEditorControls();
    if (this.editing && !rendered) {
      content.className = 'files-preview-content files-editing';
      content.innerHTML = `<textarea class="files-editor" aria-label="${t("文件内容")}" spellcheck="false" autocapitalize="off" autocomplete="off" wrap="off" data-i18n-aria-label="文件内容"></textarea>`;
      const editor = content.querySelector('textarea');
      editor.value = text;
      editor.focus();
      return;
    }
    content.className = `files-preview-content ${rendered ? 'markdown files-markdown' : 'files-text'}`;
    content.innerHTML = rendered ? markdown(text) : `<pre>${h(text)}</pre>`;
    if (!rendered) return;
    mountMermaid(content);
    for (const image of content.querySelectorAll('img[src]')) {
      const source = image.getAttribute('src');
      const path = localFilePath(this.file.path, source);
      if (path) image.src = fileUrl(this.machineId, path, true);
    }
    for (const link of content.querySelectorAll('a[href]')) {
      const href = link.getAttribute('href');
      const path = localFilePath(this.file.path, href);
      if (path) {
        link.dataset.filePath = path;
        link.href = filePreviewUrl(this.machineId, path);
      } else { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    }
  }
}
