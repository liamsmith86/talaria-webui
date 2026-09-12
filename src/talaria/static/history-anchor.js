import { useLayoutEffect, useRef } from "./lib.js";
import { beforeHistoryUpdate } from "./store.js";

function visibleBoundary(nodes, top) {
  let low = 0, high = nodes.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (nodes[mid].getBoundingClientRect().bottom <= top) low = mid + 1;
    else high = mid;
  }
  return Array.from({ length: Math.min(3, nodes.length - low) }, (_, i) => nodes[low + i]);
}

export function useHistoryAnchor(root, session, history, following) {
  const snapshot = useRef(null);
  useLayoutEffect(() => beforeHistoryUpdate(() => {
    const viewport = root.current;
    if (!viewport || following.current) return;
    const top = viewport.getBoundingClientRect().top;
    // Measure only the visible boundary, even in very long loaded histories.
    const messages = visibleBoundary(viewport.querySelectorAll(".message"), top);
    // A single reply can span many screens. Anchor its visible blocks as well
    // as its article, so omitted transient reasoning or corrected earlier
    // content does not move the prose the reader is currently looking at.
    const nodes = messages.flatMap((message) => [
      ...visibleBoundary(message.querySelectorAll(
        ".message-text .markdown-block > *, .tool-card, .reasoning > summary, " +
        ".reasoning[open] .markdown-block > *, .user-content",
      ), top),
      message,
    ]);
    snapshot.current = {
      session,
      anchors: nodes.map((node) =>
        ({ node, top: node.getBoundingClientRect().top })),
    };
  }), [root, session, following]);
  useLayoutEffect(() => {
    const saved = snapshot.current;
    snapshot.current = null;
    if (!saved || saved.session !== session || following.current) return;
    const anchor = saved.anchors.find(({ node }) => node.isConnected);
    if (anchor && root.current) {
      const delta = anchor.node.getBoundingClientRect().top - anchor.top;
      if (Math.abs(delta) > 0.5) root.current.scrollTop += delta;
    }
  }, [history, root, session, following]);
}
