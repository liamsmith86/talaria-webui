import { html, useEffect, useRef, useState, Icon, IconButton } from "./lib.js";
import { update } from "./store.js";

function matches(root, query) {
  if (!query || !root) return [];
  const ranges = [];
  const expression = new RegExp(
    query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
    "giu",
  );
  for (const container of root.querySelectorAll(
    ".message-text, .tool-content pre, .reasoning .markdown",
  )) {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let text = "",
      node;
    while ((node = walker.nextNode())) {
      if (node.parentElement.closest("button")) continue;
      nodes.push({ node, start: text.length });
      text += node.textContent;
    }
    for (const match of text.matchAll(expression)) {
      if (ranges.length >= 2000) break;
      const start = match.index;
      const end = start + match[0].length;
      const first = nodes.findLast((n) => n.start <= start);
      const last = nodes.findLast((n) => n.start < end);
      if (!first || !last) break;
      const range = new Range();
      range.setStart(first.node, start - first.start);
      range.setEnd(last.node, end - last.start);
      ranges.push(range);
    }
    if (ranges.length >= 2000) break;
  }
  return ranges;
}

export function ConversationFind({ app, root, onLoadEarlier, olderBusy }) {
  const [query, setQuery] = useState("");
  const [ranges, setRanges] = useState([]);
  const [index, setIndex] = useState(0);
  const input = useRef();
  const searchedQuery = useRef("");
  const move = useRef(false);
  useEffect(() => {
    input.current?.focus();
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => {
      const found = matches(root.current, query.trim());
      setRanges(found);
      setIndex((i) =>
        searchedQuery.current === query
          ? Math.min(i, Math.max(0, found.length - 1))
          : 0,
      );
      searchedQuery.current = query;
    }, 120);
    return () => clearTimeout(timer);
  }, [query, app.history, app.lives[app.active]?.text]);
  useEffect(() => {
    if (globalThis.CSS?.highlights && globalThis.Highlight) {
      CSS.highlights.set("talaria-find", new Highlight(...ranges));
      CSS.highlights.set(
        "talaria-current",
        new Highlight(...(ranges[index] ? [ranges[index]] : [])),
      );
    }
    const range = ranges[index];
    const parent = range?.startContainer.parentElement;
    const article = parent?.closest("article");
    article?.classList.add("find-focus");
    if (parent && move.current) {
      let details = parent.closest("details");
      while (details) {
        details.open = true;
        details = details.parentElement.closest("details");
      }
      const rect = range.getBoundingClientRect();
      const viewport = root.current;
      viewport.scrollTo({
        top:
          viewport.scrollTop +
          rect.top -
          viewport.getBoundingClientRect().top -
          viewport.clientHeight / 3,
        behavior: "instant",
      });
      move.current = false;
    }
    return () => {
      CSS.highlights?.delete("talaria-find");
      CSS.highlights?.delete("talaria-current");
      article?.classList.remove("find-focus");
    };
  }, [ranges, index]);
  function navigate(delta) {
    if (ranges.length) {
      move.current = true;
      setIndex((index + delta + ranges.length) % ranges.length);
      if (ranges.length === 1) setRanges([...ranges]);
    }
  }
  function close() {
    update({ findOpen: false });
    requestAnimationFrame(() =>
      document.querySelector('[aria-label="Find in conversation"]')?.focus(),
    );
  }
  return html`<section
    class="conversation-find"
    aria-label="Find in conversation"
  >
    <div class="find-controls">
      <${Icon} name="search" size=${17} /><input
        ref=${input}
        aria-label="Find text in conversation"
        placeholder="Find in this conversation…"
        value=${query}
        maxlength="200"
        onInput=${(e) => {
          move.current = true;
          setQuery(e.target.value);
        }}
        onKeyDown=${(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            close();
          } else if (e.key === "Enter") {
            e.preventDefault();
            navigate(e.shiftKey ? -1 : 1);
          }
        }}
      />
      <span class="find-count" role="status"
        >${query.trim()
          ? ranges.length
            ? `${index + 1} of ${ranges.length}${ranges.length === 2000 ? "+" : ""}`
            : "No matches"
          : ""}</span
      >
      <${IconButton}
        name="arrow"
        label="Previous match"
        disabled=${!ranges.length}
        onClick=${() => navigate(-1)}
      /><${IconButton}
        name="down"
        label="Next match"
        disabled=${!ranges.length}
        onClick=${() => navigate(1)}
      /><${IconButton} name="close" label="Close find" onClick=${close} />
    </div>
    <div class="find-scope">
      <span
        >${app.historyHasMore
          ? "Searching loaded messages. Earlier messages are not included yet."
          : "Searching the full loaded conversation."}</span
      >
      ${app.historyHasMore &&
      html`<button
        class="text-button"
        disabled=${olderBusy}
        onClick=${async () => {
          move.current = true;
          await onLoadEarlier();
        }}
      >
        ${olderBusy ? "Loading…" : "Include earlier messages"}
      </button>`}
    </div>
  </section>`;
}
