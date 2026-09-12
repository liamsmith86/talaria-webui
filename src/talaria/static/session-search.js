import { html, useEffect, useState } from "./lib.js";
import { api } from "./api.js";
import { openSession } from "./store.js";
import { sessionURL } from "./session-navigation.js";
import { sourceLabel } from "./content.js";

function snippet(text) {
  // Native FTS markers become text nodes and marks, never interpreted HTML.
  return text.split(/(>>>.*?<<<)/gs).map((part, index) =>
    part.startsWith(">>>") && part.endsWith("<<<")
      ? html`<mark key=${index}>${part.slice(3, -3)}</mark>` : part);
}

export function SessionSearch({ query }) {
  const [result, setResult] = useState({ query: "", data: [] });
  const [offset, setOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const response = await api(`/search?q=${encodeURIComponent(query)}&offset=${offset}`, { signal: controller.signal });
        if (!controller.signal.aborted) setResult((previous) => ({
          ...response, query, busy: false,
          data: offset && previous.query === query ? [...previous.data, ...response.data] : response.data,
        }));
      } catch (error) {
        if (!controller.signal.aborted) setResult((previous) => ({
          query, data: previous.query === query ? previous.data : [], error: error.message,
        }));
      }
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, offset, retry]);
  const current = result.query === query;
  const busy = !current || result.busy;
  return html`<section class="history-search" aria-label="Matching messages" aria-busy=${!!busy}>
    <div class="session-group">Messages</div>
    ${current && result.data.map((hit) => html`<a key=${`${hit.session_id}:${hit.id}`}
      class="history-search-hit" href=${sessionURL(hit.session_id, hit.id)}
      onClick=${(event) => {
        if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        openSession(hit.session_id, null, false, String(hit.id));
      }}>
      <strong>${hit.title}</strong><span>${snippet(hit.snippet)}</span>
      <small>${sourceLabel(hit.source)}${hit.source ? " · " : ""}${hit.role === "user" ? "You" : hit.role === "tool" ? "Tool" : "Agent"}</small>
    </a>`)}
    <div class="sidebar-empty" role="status">
      ${busy ? "Searching…" : result.error || (!result.data.length ? "No matching messages." : "")}
    </div>
    ${current && result.error && html`<button class="load-more" onClick=${() => {
      setResult({ ...result, error: null, busy: true }); setRetry(retry + 1);
    }}>Retry search</button>`}
    ${current && result.has_more && result.next_offset <= 10000 && html`<button class="load-more" disabled=${busy}
      onClick=${() => { setResult({ ...result, busy: true }); setOffset(result.next_offset); }}>
      ${busy ? "Searching…" : "More matches"}</button>`}
  </section>`;
}
