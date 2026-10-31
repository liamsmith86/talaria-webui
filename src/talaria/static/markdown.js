import { html, memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "./lib.js";
import { marked } from "./vendor/marked.js";
import DOMPurify from "./vendor/purify.js";
import { MarkdownStream, flatList } from "./markdown-stream.js";
import { StreamText, useStreamReveal } from "./stream-reveal.js";
import { t, useLanguage } from "./i18n.js";

const renderer = new marked.Renderer();
const escape = (text) =>
  text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
renderer.html = ({ text }) => escape(text);
renderer.image = ({ text, href }) =>
  `<a href="${escape(href)}"${text ? "" : ' data-talaria-image-fallback="true"'}>${escape(text || t("Image attachment"))}</a>`;
const table = renderer.table;
renderer.table = function (token) {
  return `<div class="table-scroll" role="region" aria-label="${escape(t("Table"))}" tabindex="0">${table.call(this, token)}</div>`;
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
  return `<div class="code-block"><div class="code-heading"><span>${language}</span><button type="button" data-copy-code="true">${escape(t("Copy code"))}</button></div><pre tabindex="0" role="region" aria-label="${escape(t("{language} code", { language }))}"><code>${highlighted}</code></pre></div>`;
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

const inline = (tokens) => sanitize(marked.Parser.parseInline(tokens, marked.defaults));
const TableRow = memo(({ cells }) => html`<tr>
  ${cells.map((cell) => {
    const tag = cell.header ? "th" : "td";
    return html`<${tag}
      align=${cell.align || undefined}
      dangerouslySetInnerHTML=${{ __html: inline(cell.tokens) }}
    />`;
  })}
</tr>`);
const ListItem = memo(({ item }) => html`<li
  dangerouslySetInnerHTML=${{ __html: inline(item.tokens[0]?.tokens || []) }}
/>`);
const Block = memo(({ block }) => {
  if (block.content) return html`<div class="markdown-block">${block.content}</div>`;
  if (block.parts)
    return html`<div class="markdown-block">
      <p>
        ${block.parts.map((part, i) => html`<span
          class="markdown-inline"
          key=${i}
          dangerouslySetInnerHTML=${{ __html: part.markup }}
        />`)}
      </p>
    </div>`;
  return html`<div class="markdown-block" dangerouslySetInnerHTML=${{ __html: block.markup }} />`;
});

function copyLabel(button) {
  if (button.dataset.copyState === "copied") return t("Copied");
  if (button.dataset.copyState === "failed") return t("Select text to copy");
  return t("Copy code");
}

function localizeControls(root) {
  for (const button of root.querySelectorAll("[data-copy-code]"))
    button.textContent = copyLabel(button);
  for (const block of root.querySelectorAll(".code-block")) {
    const language = block.querySelector(".code-heading span").textContent;
    block.querySelector("pre").setAttribute("aria-label", t("{language} code", { language }));
  }
  for (const table of root.querySelectorAll(".table-scroll"))
    table.setAttribute("aria-label", t("Table"));
  for (const image of root.querySelectorAll("[data-talaria-image-fallback]"))
    image.textContent = t("Image attachment");
}

export function Markdown({
  text,
  streaming = false,
  deferHighlight = false,
  smooth = false,
}) {
  const { locale } = useLanguage();
  text = useStreamReveal(text || "", smooth && streaming);
  const root = useRef();
  const streamed = useRef(false);
  const [nearby, setNearby] = useState(false);
  const highlightVisible =
    !deferHighlight || nearby || !globalThis.IntersectionObserver;
  useEffect(() => {
    if (highlightVisible || !root.current) return;
    // Saved text without code has nothing to highlight on approach.
    if (!streamed.current && !root.current.querySelector(".code-block")) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNearby(true);
          observer.disconnect();
        }
      },
      // The margin only reaches past the viewport from the element that scrolls.
      { root: root.current.closest(".conversation-viewport"), rootMargin: "300px" },
    );
    observer.observe(root.current);
    return () => observer.disconnect();
  }, [text, highlightVisible]);
  // A saved message is highlighted in place below, so approaching it must not parse it again.
  const highlightBlocks = (streamed.current || streaming) && highlightVisible;
  const previous = useRef([]);
  const scanner = useRef(new MarkdownStream());
  const blocks = useMemo(() => {
    streamed.current ||= streaming;
    // Saved messages need one sanitization pass. Offscreen code stays readable
    // as plain text, with syntax highlighting added as it approaches the viewport.
    if (!streamed.current)
      return [{ markup: renderMarkdown(text, !deferHighlight) }];
    const tokens = scanner.current.read(text || "", streaming);
    const links = JSON.stringify(tokens.links);
    const visible = tokens.filter((token) => token.type !== "space");
    const next = visible.map((token, index) => {
      const highlight =
        highlightBlocks && (!streaming || index < visible.length - 1);
      const old = previous.current[index];
      if (
        old?.raw === token.raw &&
        old.links === links &&
        old.highlight === highlight
      )
        return old;
      const block = { raw: token.raw, links, highlight, token };
      if (token.streamPlain) {
        block.content = html`<p>
          ${
            smooth
              ? html`<${StreamText}
                  text=${token.text}
                  active=${streaming && index === visible.length - 1}
                />`
              : token.text
          }
        </p>`;
      } else if (token.type === "code") {
        const language = (token.lang || "text")
          .split(/\s/)[0]
          .replace(/[^a-z0-9-]/gi, "")
          .slice(0, 30);
        const grammar = window.Prism?.languages[language];
        const highlighted = highlight && grammar && token.text.length < 10000;
        // Keep the VNode's baseline stable while an imperative copied/failed
        // label survives a language switch and subsequent streaming chunks.
        block.copyText = old?.copyText || t("Copy code");
        block.content = html`<div class="code-block">
          <div class="code-heading">
            <span>${language}</span
            ><button type="button" data-copy-code="true">${block.copyText}</button>
          </div>
          <pre tabindex="0" role="region" aria-label=${t("{language} code", { language })}><code
            dangerouslySetInnerHTML=${highlighted ? { __html: sanitize(window.Prism.highlight(token.text, grammar, language)) } : undefined}
          >${
            highlighted
              ? undefined
              : smooth
                ? html`<${StreamText}
                    text=${token.text}
                    active=${streaming && index === visible.length - 1}
                  />`
                : token.text
          }</code></pre>
        </div>`;
      } else if (token.type === "table") {
        if (old?.links === links && old.token?.type === "table") {
          const same = (a, b) =>
            a &&
            a.length === b.length &&
            a.every(
              (cell, i) => cell.text === b[i].text && cell.align === b[i].align,
            );
          if (same(old.token.header, token.header))
            token.header = old.token.header;
          token.rows = token.rows.map((cells, i) =>
            same(old.token.rows[i], cells) ? old.token.rows[i] : cells,
          );
        }
        block.content = html`<div
          class="table-scroll"
          role="region"
          aria-label=${t("Table")}
          tabindex="0"
        >
          <table>
            <thead>
              <${TableRow} key="header" cells=${token.header} />
            </thead>
            ${
              token.rows.length > 0 &&
              html`<tbody>
                ${token.rows.map((cells, i) => html`<${TableRow} key=${i} cells=${cells} />`)}
              </tbody>`
            }
          </table>
        </div>`;
      } else if (flatList(token)) {
        if (old?.links === links && old.token && flatList(old.token))
          token.items = token.items.map((item, i) =>
            old.token.items[i]?.text === item.text ? old.token.items[i] : item,
          );
        const tag = token.ordered ? "ol" : "ul";
        block.content = html`<${tag} start=${token.ordered && token.start !== 1 ? token.start : undefined}>
          ${token.items.map((item, i) => html`<${ListItem} key=${i} item=${item} />`)}
        </${tag}>`;
      }
      if (block.content) return block;
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
  }, [text, streaming, highlightBlocks, deferHighlight, smooth]);
  useLayoutEffect(() => {
    // Update owned labels in place. Locale changes must not reparse Markdown,
    // replace code nodes, or discard focus, selection, scrolling, and copy state.
    localizeControls(root.current);
  }, [locale]);
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
        code.innerHTML = sanitize(
          window.Prism.highlight(source, grammar, language),
        );
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
        button.dataset.copyState = "copied";
        button.textContent = copyLabel(button);
        setTimeout(() => {
          if (button.isConnected) {
            button.dataset.copyState = "ready";
            button.textContent = copyLabel(button);
          }
        }, 1800);
      } catch {
        button.dataset.copyState = "failed";
        button.textContent = copyLabel(button);
      }
    }}
  >
    ${blocks.map((block, index) => html`<${Block} key=${index} block=${block} />`)}
  </div>`;
}
