import { html, useMemo } from "./lib.js";
import { marked } from "./vendor/marked.js";
import DOMPurify from "./vendor/purify.js";

const renderer = new marked.Renderer();
const escape = (text) =>
  text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
renderer.html = ({ text }) => escape(text);
renderer.image = ({ text, href }) =>
  `<a href="${escape(href)}">${escape(text || "Image attachment")}</a>`;
const table = renderer.table;
renderer.table = function (token) {
  return `<div class="table-scroll" role="region" aria-label="Table" tabindex="0">${table.call(this, token)}</div>`;
};
renderer.code = ({ text, lang }) => {
  const language = (lang || "text")
    .split(/\s/)[0]
    .replace(/[^a-z0-9-]/gi, "")
    .slice(0, 30);
  const grammar = window.Prism?.languages[language];
  const highlighted =
    grammar && text.length < 50000
      ? window.Prism.highlight(text, grammar, language)
      : escape(text);
  return `<div class="code-block"><div class="code-heading"><span>${language}</span><button type="button" data-copy-code="true">Copy code</button></div><pre tabindex="0" role="region" aria-label="${language} code"><code>${highlighted}</code></pre></div>`;
};
marked.use({ renderer, gfm: true, breaks: false });

export function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ""), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["img", "input", "form", "style", "iframe"],
    FORBID_ATTR: ["style", "id", "name"],
  });
}
export function Markdown({ text, streaming = false }) {
  const markup = useMemo(() => renderMarkdown(text), [text]);
  return html`<div
    class=${`markdown ${streaming ? "streaming" : ""}`}
    onClick=${async (e) => {
      const button = e.target.closest("[data-copy-code]");
      if (!button || !e.currentTarget.contains(button)) return;
      try {
        await navigator.clipboard.writeText(
          button.closest(".code-block").querySelector("code").textContent,
        );
        button.textContent = "Copied";
        setTimeout(() => {
          if (button.isConnected) button.textContent = "Copy code";
        }, 1800);
      } catch {
        button.textContent = "Select text to copy";
      }
    }}
    dangerouslySetInnerHTML=${{ __html: markup }}
  />`;
}
