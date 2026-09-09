import { html, useMemo } from './lib.js';
import { marked } from './vendor/marked.js';
import DOMPurify from './vendor/purify.js';

const renderer = new marked.Renderer();
renderer.html = ({ text }) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
renderer.image = ({ text, href }) => `<a href="${DOMPurify.sanitize(href, { ALLOWED_TAGS: [] }).replace(/"/g, '&quot;')}">${DOMPurify.sanitize(text || 'Image', { ALLOWED_TAGS: [] })}</a>`;
marked.use({ renderer, gfm: true, breaks: false });

export function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ''), {
    USE_PROFILES: { html: true }, FORBID_TAGS: ['img', 'input', 'form', 'style', 'iframe'],
    FORBID_ATTR: ['style', 'id', 'name'],
  });
}
export function Markdown({ text, streaming = false }) {
  const markup = useMemo(() => renderMarkdown(text), [text]);
  return html`<div class=${`markdown ${streaming ? 'streaming' : ''}`} dangerouslySetInnerHTML=${{ __html: markup }}/>`;
}
