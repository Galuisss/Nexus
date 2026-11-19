let runtime;
let rendering = Promise.resolve();
let sequence = 0;
const themes = new WeakMap();
const theme = () => getComputedStyle(document.documentElement).colorScheme === 'dark' ? 'dark' : 'default';

function loadRuntime() {
  return runtime ||= import('./vendor/mermaid.js?v=145').then(module => module.default).catch(error => {
    runtime = null;
    throw error;
  });
}

export function mountMermaid(container) {
  const currentTheme = theme();
  for (const element of container.querySelectorAll('.mermaid-block[data-mermaid-source]')) {
    if (themes.get(element) === currentTheme) continue;
    themes.set(element, currentTheme);
    rendering = rendering.then(async () => {
      if (!element.isConnected || themes.get(element) !== currentTheme) return;
      try {
        const mermaid = await loadRuntime();
        if (!element.isConnected) return;
        mermaid.initialize({
          startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
          theme: currentTheme, fontFamily: getComputedStyle(element).fontFamily,
        });
        const { svg } = await mermaid.render(`nexus-mermaid-${++sequence}`, decodeURIComponent(element.dataset.mermaidSource));
        if (element.isConnected && themes.get(element) === currentTheme) element.innerHTML = svg;
      } catch (error) {
        // Keep the source, or the previous diagram, visible when rendering fails.
        console.error('Nexus Mermaid:', error);
      }
    });
  }
}

export function preserveMermaid(current, next) {
  const blocks = new Map();
  for (const element of current.querySelectorAll('.mermaid-block[data-mermaid-source]')) {
    const source = element.dataset.mermaidSource;
    if (!blocks.has(source)) blocks.set(source, []);
    blocks.get(source).push(element);
  }
  for (const element of next.querySelectorAll('.mermaid-block[data-mermaid-source]')) {
    const previous = blocks.get(element.dataset.mermaidSource)?.shift();
    if (previous) element.replaceWith(previous);
  }
}

new MutationObserver(() => mountMermaid(document)).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => mountMermaid(document));
