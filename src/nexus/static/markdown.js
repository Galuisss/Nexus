import { t } from './i18n.js?v=145';
import { marked, Renderer } from './vendor/marked.js?v=145';
import DOMPurify from './vendor/purify.js?v=145';
import katex from './vendor/katex.js?v=145';
import { localFileReference } from './files.js?v=145';

const delimiters = [
  { left: '$$', right: '$$', displayMode: true },
  { left: '\\[', right: '\\]', displayMode: true },
  { left: '\\(', right: '\\)', displayMode: false },
  { left: '$', right: '$', displayMode: false },
];
const formulas = new Map();

function mathToken(source) {
  const delimiter = delimiters.find(({ left }) => source.startsWith(left));
  if (!delimiter) return;
  const { left, right, displayMode } = delimiter;
  if (left === '$' && /\s/.test(source[1] || '')) return;
  let braces = 0;
  for (let i = left.length; i < source.length; i++) {
    if (!braces && source.startsWith(right, i)) {
      if (left === '$' && (/\s/.test(source[i - 1]) || /\d/.test(source[i + 1] || ''))) return;
      return { raw: source.slice(0, i + right.length), text: source.slice(left.length, i), displayMode };
    }
    if (left === '$' && source[i] === '\n') return;
    if (source[i] === '\\') i++;
    else if (source[i] === '{') braces++;
    else if (source[i] === '}') braces--;
  }
}

function renderMath(token) {
  const key = `${token.displayMode}:${token.text}`;
  if (!formulas.has(key)) {
    formulas.set(key, katex.renderToString(token.text, { displayMode: token.displayMode, throwOnError: false }));
    if (formulas.size > 128) formulas.delete(formulas.keys().next().value);
  }
  return formulas.get(key);
}

marked.use({
  breaks: true,
  gfm: true,
  renderer: {
    code(token) {
      const language = token.lang?.trim().split(/\s+/)[0];
      if (!language) return false;
      if (language.toLowerCase() === 'mermaid') {
        const fence = token.raw.match(/^ {0,3}(`{3,}|~{3,})/);
        const closed = fence && new RegExp(`\\n {0,3}${fence[1][0]}{${fence[1].length},}[\\t ]*(?:\\n)?$`).test(token.raw);
        if (!this.options.streaming || closed) {
          const source = encodeURIComponent(token.text);
          return `<div class="mermaid-block" data-mermaid-source="${source}">${Renderer.prototype.code.call(this, token)}</div>
`;
        }
      }
      return `<div class="code-block"><div class="code-language">${this.text({ text: language })}</div>${Renderer.prototype.code.call(this, token)}</div>
`;
    },
  },
  walkTokens(token) {
    if (token.type === 'link') token.href = localFileReference(token.href) || token.href;
  },
  extensions: [
    {
      name: 'visualization',
      level: 'block',
      start: source => source.match(/^ {0,3}visualize/m)?.index,
      tokenizer(source) {
        const match = source.match(/^ {0,3}visualize([^\n]*?)[\t ]*(?:\n|$)/);
        if (!match) return;
        try {
          const reference = JSON.parse(match[1]);
          if (typeof reference.path === 'string' && reference.path.startsWith('/')) {
            return { ...reference, type: 'visualization', raw: match[0] };
          }
        } catch { /* Incomplete references remain text while streaming. */ }
      },
      renderer(token) {
        const escape = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
        return `<div class="visualization" data-visualization-path="${escape(token.path)}" data-visualization-mode="${token.mode === 'wide' ? 'wide' : 'regular'}" data-visualization-title="${escape(token.title || t("可视化"))}"><span class="spinner" role="status" aria-label="${t("正在加载可视化")}" data-i18n-aria-label="正在加载可视化"></span></div>
`;
      },
    },
    {
      name: 'mathBlock',
      level: 'block',
      start: source => source.match(/^ {0,3}(?:\$\$|\\\[)/m)?.index,
      tokenizer(source) {
        const indent = source.match(/^ {0,3}/)[0];
        const token = mathToken(source.slice(indent.length));
        if (!token?.displayMode) return;
        const trailing = source.slice(indent.length + token.raw.length).match(/^[\t ]*(?:\n|$)/);
        if (trailing) return { ...token, type: 'mathBlock', raw: indent + token.raw + trailing[0] };
      },
      renderer: token => `${renderMath(token)}
`,
    },
    {
      name: 'mathInline',
      level: 'inline',
      start: source => source.search(/\$|\\\(|\\\[/),
      tokenizer(source) {
        if (this.lexer.state.inRawBlock) return;
        const token = mathToken(source);
        if (token) return { ...token, type: 'mathInline' };
        if (source.startsWith('$$')) return { type: 'text', raw: '$$', text: '$$' };
      },
      renderer: renderMath,
    },
  ],
});

export const markdown = (text, { streaming = false } = {}) => DOMPurify.sanitize(marked.parse(String(text || ''), { streaming }), {
  FORBID_TAGS: ['style', 'form', 'iframe'],
  ADD_TAGS: ['semantics', 'annotation'],
});
