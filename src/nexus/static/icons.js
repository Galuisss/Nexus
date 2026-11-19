const paths = {
  'messages': '<path d="M21 11a7 7 0 0 1-7 7H7l-5 3 2-6a7 7 0 0 1-1-4 8 8 0 0 1 9-8 8 8 0 0 1 9 8Z"/><path d="M8 9h8m-8 4h5"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-16a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 4v2"/>',
  user: '<circle cx="12" cy="7" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  'square-pen': '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="m16 3 5 5M10 14l-1 4 4-1L22 8a3.5 3.5 0 0 0-5-5Z"/>',
  inbox: '<path d="m4 4-2 10v6h20v-6L20 4Z"/><path d="M2 14h6l2 3h4l2-3h6"/>',
  'circle-help': '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2-3 4m.1 3h0"/>',
  archive: '<rect x="3" y="3" width="18" height="4" rx="1"/><path d="M5 7v14h14V7m-10 4h6"/>',
  'archive-restore': '<rect x="3" y="3" width="18" height="4" rx="1"/><path d="M5 7v14h14V7m-7 10v-7m-3 3 3-3 3 3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  zap: '<path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z"/>',
  'corner-down-left': '<path d="M20 4v8a3 3 0 0 1-3 3H4m5-5-5 5 5 5"/>',
  settings: '<path d="m9 3-.7 2.2-2 .9L4 5.7 2 9l1.7 1.8v2.4L2 15l2 3.3 2.3-.4 2 .9L9 21h6l.7-2.2 2-.9 2.3.4 2-3.3-1.7-1.8v-2.4L22 9l-2-3.3-2.3.4-2-.9L15 3Z"/><circle cx="12" cy="12" r="3"/>',
  'sun-moon': '<path d="M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9Z"/><path d="M19 3v4m-2-2h4M16 10h0"/>',
  'panel-left': '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/>',
  'panel-right': '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M15 3v18"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-9-9c2.5 0 4.7 1 6.3 2.7L21 8"/><path d="M21 3v5h-5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  sparkles: '<path d="m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7Zm7-1v4m-2-2h4"/>',
  'arrow-right': '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  'arrow-left': '<path d="M19 12H5m6-6-6 6 6 6"/>',
  'arrow-up': '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  'list-checks': '<path d="m3 6 1 1 2-2m-3 8 1 1 2-2m-3 8 1 1 2-2M10 6h11M10 13h11M10 20h11"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  ellipsis: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  pencil: '<path d="m16 3 5 5M3 21l5-1L21 7a3.5 3.5 0 0 0-5-5L3 15Z"/>',
  play: '<path d="m7 4 14 8-14 8Z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  'git-fork': '<circle cx="6" cy="4" r="2"/><circle cx="18" cy="4" r="2"/><circle cx="12" cy="20" r="2"/><path d="M6 6v3a6 6 0 0 0 6 6m6-9v3a6 6 0 0 1-6 6v3"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  square: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  'chevron-up': '<path d="m6 15 6-6 6 6"/>',
  'chevron-down': '<path d="m6 9 6 6 6-6"/>',
  'chevron-right': '<path d="m9 6 6 6-6 6"/>',
  sliders: '<path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  x: '<path d="m6 6 12 12M6 18 18 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  monitor: '<rect x="3" y="3" width="18" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h0m0 11h0"/>',
  folder: '<path d="M3 5h6l2 3h10v13H3Z"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-7 5 6"/>',
  'circle-alert': '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h0"/>',
  'file-code': '<path d="M14 2H4v20h16V8l-6-6v6h6m-10 4-3 3 3 3m4-6 3 3-3 3"/>',
  globe: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  brain: '<path d="M12 5c-4-5-9 0-6 4-5 1-4 8 0 8 0 5 6 5 6 1V5Zm0 0c4-5 9 0 6 4 5 1 4 8 0 8 0 5-6 5-6 1"/>',
  'external-link': '<path d="M14 3h7v7m0-7L10 14m0-11H3v18h18v-7"/>',
  trash: '<path d="M3 6h18m-16 0 1 15h12l1-15M9 6V3h6v3m-5 4v7m4-7v7"/>',
  link: '<path d="m10 13 4-4m-6 6-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 2 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
  moon: '<path d="M20.8 13A8.5 8.5 0 0 1 11 3.2 9 9 0 1 0 20.8 13Z"/>',
};

export function icon(name, className = '') {
  return `<svg class="icon ${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths['file-code']}</svg>`;
}

export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(el => {
    el.innerHTML = icon(el.dataset.icon);
    el.removeAttribute('data-icon');
  });
}
