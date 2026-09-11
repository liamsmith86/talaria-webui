import { html, useEffect, useRef, useState, Icon, IconButton } from "./lib.js";
import { update } from "./store.js";

const searchable = ".message-text, .tool-content pre, .reasoning .markdown";

function matches(root, query, cache) {
  if (!query || !root) return [];
  const ranges = [];
  const expression = new RegExp(
    query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
    "giu",
  );
  for (const container of root.querySelectorAll(searchable)) {
    let indexed = cache.get(container);
    if (!indexed) {
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
      const nodes = [];
      let text = "",
        node;
      while ((node = walker.nextNode())) {
        if (!node.textContent || node.parentElement.closest("button")) continue;
        nodes.push({ node, start: text.length });
        text += node.textContent;
      }
      indexed = { nodes, text };
      cache.set(container, indexed);
    }
    const { nodes, text } = indexed;
    let firstIndex = 0,
      lastIndex = 0;
    for (const match of text.matchAll(expression)) {
      if (ranges.length >= 2000) break;
      const start = match.index;
      const end = start + match[0].length;
      while (nodes[firstIndex + 1]?.start <= start) firstIndex++;
      while (nodes[lastIndex + 1]?.start < end) lastIndex++;
      const first = nodes[firstIndex],
        last = nodes[lastIndex];
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

function revealMatch(range, viewport) {
  const parent = range.startContainer.parentElement;
  let details = parent.closest("details");
  while (details) {
    details.open = true;
    details = details.parentElement.closest("details");
  }
  // A match can be clipped inside a long message or a tool/code scroll region.
  for (let el = parent; el; el = el.parentElement) {
    const rect = range.getBoundingClientRect(),
      bounds = el.getBoundingClientRect();
    if (el.scrollHeight > el.clientHeight)
      el.scrollTop +=
        rect.top - bounds.top - el.clientTop - el.clientHeight / 3;
    if (el.scrollWidth > el.clientWidth)
      el.scrollLeft +=
        rect.left - bounds.left - el.clientLeft - el.clientWidth / 3;
    if (el === viewport) break;
  }
}

export function ConversationFind({ app, root, onLoadEarlier, olderBusy }) {
  const [query, setQuery] = useState("");
  const [ranges, setRanges] = useState([]);
  const [index, setIndex] = useState(0);
  const input = useRef();
  const searchedQuery = useRef("");
  const move = useRef(false);
  const search = useRef({ query: "", timer: 0, cache: new WeakMap() });
  search.current.query = query;
  function schedule() {
    const pending = search.current;
    if (pending.timer) return;
    pending.timer = setTimeout(() => {
      pending.timer = 0;
      const found = matches(root.current, pending.query.trim(), pending.cache);
      setRanges(found);
      setIndex((i) =>
        searchedQuery.current === pending.query
          ? Math.min(i, Math.max(0, found.length - 1))
          : 0,
      );
      searchedQuery.current = pending.query;
    }, 120);
  }
  useEffect(() => {
    input.current?.focus();
  }, []);
  useEffect(() => {
    // Index visible DOM, not network text: reveal pacing and deferred code
    // highlighting can update text nodes independently of the store. Retain
    // unchanged containers and invalidate only where content actually changed.
    const cache = search.current.cache;
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        const target =
          record.target.nodeType === Node.ELEMENT_NODE
            ? record.target
            : record.target.parentElement;
        const container = target?.closest(searchable);
        if (container) cache.delete(container);
        else
          for (const node of record.addedNodes) {
            if (node.nodeType !== Node.ELEMENT_NODE) continue;
            if (node.matches(searchable)) cache.delete(node);
            for (const child of node.querySelectorAll(searchable))
              cache.delete(child);
          }
      }
      // Throttle DOM updates instead of repeatedly postponing the scan until
      // a stream pauses. Search results stay current during continuous output.
      if (search.current.query.trim()) schedule();
    });
    observer.observe(root.current, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    return () => {
      observer.disconnect();
      clearTimeout(search.current.timer);
      search.current.timer = 0;
    };
  }, [root]);
  useEffect(() => {
    clearTimeout(search.current.timer);
    search.current.timer = 0;
    schedule();
  }, [query]);
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
      revealMatch(range, root.current);
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
      document.querySelector('[aria-label="Find in session"]')?.focus(),
    );
  }
  return html`<section
    class="conversation-find"
    aria-label="Find in session"
  >
    <div class="find-controls">
      <${Icon} name="search" size=${17} /><input
        ref=${input}
        aria-label="Find text in session"
        placeholder="Find in this session…"
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
          : "Searching the full loaded session."}</span
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
