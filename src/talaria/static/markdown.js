import { html, useEffect, useMemo, useRef, useState } from "./lib.js";
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
function code({ text, lang }, highlight = true) {
  const language = (lang || "text")
    .split(/\s/)[0]
    .replace(/[^a-z0-9-]/gi, "")
    .slice(0, 30);
  const grammar = window.Prism?.languages[language];
  const highlighted =
    // Large highlighted blocks create thousands of spans. Keep their complete
    // source readable/copyable without a long main-thread pause on mobile.
    highlight && grammar && text.length < 10000
      ? window.Prism.highlight(text, grammar, language)
      : escape(text);
  return `<div class="code-block"><div class="code-heading"><span>${language}</span><button type="button" data-copy-code="true">Copy code</button></div><pre tabindex="0" role="region" aria-label="${language} code"><code>${highlighted}</code></pre></div>`;
}
renderer.code = (token) => code(token);
marked.use({ renderer, gfm: true, breaks: true });
const streamingRenderer = new marked.Renderer();
// Reuse all configured renderers, only deferring syntax highlighting of the
// growing final block. It is highlighted when complete or streaming ends.
Object.assign(streamingRenderer, renderer, {
  code: (token) => code(token, false),
});

function sanitize(markup) {
  return DOMPurify.sanitize(markup, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["img", "input", "form", "style", "iframe"],
    FORBID_ATTR: ["style", "id", "name"],
  });
}
export function renderMarkdown(text, highlight = true) {
  return sanitize(
    marked.parse(text || "", {
      renderer: highlight ? marked.defaults.renderer : streamingRenderer,
    }),
  );
}
export function Markdown({ text, streaming = false, deferHighlight = false }) {
  const root = useRef();
  const streamed = useRef(false);
  const [nearby, setNearby] = useState(false);
  const highlightVisible =
    !deferHighlight || nearby || !globalThis.IntersectionObserver;
  useEffect(() => {
    if (highlightVisible || !root.current) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNearby(true);
          observer.disconnect();
        }
      },
      { rootMargin: "300px" },
    );
    observer.observe(root.current);
    return () => observer.disconnect();
  }, [highlightVisible]);
  const previous = useRef([]);
  const blocks = useMemo(() => {
    streamed.current ||= streaming;
    // Saved messages need one sanitization pass. Offscreen code stays readable
    // as plain text, with syntax highlighting added as it approaches the viewport.
    if (!streamed.current)
      return [{ markup: renderMarkdown(text, !deferHighlight) }];
    // Lex the complete source so setext headings, lists, and references that
    // arrive later retain Marked's semantics. Cache only this message's current
    // blocks; no growing global cache and no reparsing/highlighting old HTML.
    const tokens = marked.lexer(text || "");
    const links = JSON.stringify(tokens.links);
    const visible = tokens.filter((token) => token.type !== "space");
    const next = visible.map((token, index) => {
      const highlight =
        highlightVisible && (!streaming || index < visible.length - 1);
      const old = previous.current[index];
      if (
        old?.raw === token.raw &&
        old.links === links &&
        old.highlight === highlight
      )
        return old;
      if (token.type === "paragraph" && token.raw.length > 4096) {
        const parts = [];
        // Keep complete inline tokens together (links, emphasis, code, etc.).
        // Re-lexing above still resolves syntax spanning successive deltas.
        for (let offset = 0; offset < token.tokens.length; offset += 64) {
          const group = token.tokens.slice(offset, offset + 64);
          const raw = group.map((item) => item.raw).join("");
          const prior = old?.links === links && old.parts?.[parts.length];
          parts.push(
            prior?.raw === raw
              ? prior
              : {
                  raw,
                  markup: sanitize(
                    marked.Parser.parseInline(group, marked.defaults),
                  ),
                },
          );
        }
        return { raw: token.raw, links, highlight, parts };
      }
      const markup = sanitize(
        marked.parser([token], {
          ...marked.defaults,
          renderer: highlight ? marked.defaults.renderer : streamingRenderer,
        }),
      );
      return { raw: token.raw, links, highlight, markup };
    });
    previous.current = next;
    return next;
  }, [text, streaming, highlightVisible, deferHighlight]);
  useEffect(() => {
    if (streamed.current || !deferHighlight || !highlightVisible) return;
    // Enrich code in place: replacing the whole saved message would discard
    // keyboard focus, code scrolling, and expanded controls when it becomes visible.
    for (const block of root.current.querySelectorAll(".code-block")) {
      const code = block.querySelector("code");
      const language = block.querySelector(".code-heading span").textContent;
      const grammar = window.Prism?.languages[language];
      const source = code.textContent;
      if (grammar && source.length < 10000)
        code.innerHTML = sanitize(window.Prism.highlight(source, grammar, language));
    }
  }, [text, deferHighlight, highlightVisible]);
  return html`<div
    ref=${root}
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
  >
    ${blocks.map((block, index) =>
      block.parts
        ? html`<div class="markdown-block" key=${index}>
            <p>
              ${block.parts.map(
                (part, i) =>
                  html`<span
                    class="markdown-inline"
                    key=${i}
                    dangerouslySetInnerHTML=${{ __html: part.markup }}
                  />`,
              )}
            </p>
          </div>`
        : html`<div
            class="markdown-block"
            key=${index}
            dangerouslySetInnerHTML=${{ __html: block.markup }}
          />`,
    )}
  </div>`;
}
