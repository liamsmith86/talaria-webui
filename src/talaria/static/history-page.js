import { api } from "./api.js";

const endpoint = (id, offset, limit) =>
  `/sessions/${encodeURIComponent(id)}/messages?offset=${offset}&limit=${limit}`;
const identity = (page) => JSON.stringify([page.session_id, page.data.map((row) => row.id)]);

// Revalidate only the loaded window, not the whole session. Native Hermes uses
// offsets from the latest message; a fresh window also removes edited/deleted
// rows without maintaining a second authoritative transcript in the browser.
export async function historyWindow(id, previous = [], signal) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const result = await readWindow(id, previous, signal);
    if (result.canonical || !result.verify) return result;
    // A growing tail shifts every offset. Before combining multiple pages,
    // verify that their shared origin stayed put; retry once, never loop forever.
    const check = await api(endpoint(id, 0, result.verify.limit), { signal });
    if (identity(check) === result.verify.identity) return result;
  }
  throw new Error("Session history changed while loading. Please try again.");
}

async function readWindow(id, previous, signal) {
  const first = previous[0]?.id;
  const numeric = Number.isSafeInteger(first);
  // Bound catch-up even after a tab has slept through thousands of tool calls.
  const budget = previous.length ? previous.length + 1000 : 100;
  const pages = [];
  let offset = 0, page, origin, trim = 0, reached = false;
  do {
    const limit = Math.min(500, budget - offset, Math.max(100, previous.length + 100));
    page = await api(endpoint(id, offset, limit), { signal });
    if (page.session_id && page.session_id !== id) {
      // Never combine rows from opposite sides of native compaction.
      return { ...page, data: page.data || [], canonical: page.session_id };
    }
    const incoming = page.data || [];
    origin ??= { identity: identity(page), limit };
    pages.push(incoming);
    const next = page.next_offset ?? offset + incoming.length;
    if (page.has_more && next <= offset)
      throw new Error("Hermes did not advance the history page. Please try again.");
    offset = next;
    const crossed = numeric && incoming[0]?.id <= first;
    const index = crossed
      ? incoming.findIndex((row) => row.id >= first)
      : first == null ? -1 : incoming.findIndex((row) => row.id === first);
    if (crossed || index >= 0) {
      // The old first row may have been deleted at a page boundary. If even
      // the newest page precedes it, Hermes rewound beyond the loaded window.
      trim = index >= 0 ? index : pages.length > 1 ? incoming.length : 0;
      reached = true;
      break;
    }
  } while (previous.length && page.has_more && offset < budget);
  if (numeric && !reached && page.has_more && pages.at(-1)[0]?.id > first)
    throw new Error("A lot of new history is available. Reopen the session to load the latest messages.");
  return {
    data: pages.toReversed().flat().slice(trim),
    has_more: trim > 0 || !!page.has_more, next_offset: offset - trim,
    verify: pages.length > 1 ? origin : null,
  };
}
