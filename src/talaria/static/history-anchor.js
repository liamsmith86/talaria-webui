import { useLayoutEffect, useRef } from "./lib.js";
import { beforeHistoryUpdate } from "./store.js";

export function useHistoryAnchor(root, session, history, following) {
  const snapshot = useRef(null);
  useLayoutEffect(() => beforeHistoryUpdate(() => {
    const viewport = root.current;
    if (!viewport || following.current) return;
    const nodes = viewport.querySelectorAll(".message");
    const top = viewport.getBoundingClientRect().top;
    // Measure only the visible boundary, even in very long loaded histories.
    let low = 0, high = nodes.length;
    while (low < high) {
      const mid = (low + high) >>> 1;
      if (nodes[mid].getBoundingClientRect().bottom <= top) low = mid + 1;
      else high = mid;
    }
    snapshot.current = {
      session,
      anchors: Array.from(nodes).slice(low, low + 3).map((node) =>
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
