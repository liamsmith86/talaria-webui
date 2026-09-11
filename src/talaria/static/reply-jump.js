import { useEffect, useState } from "./lib.js";

// Only measure replies intersecting the viewport, once per frame. Streaming
// text does not rescan the transcript or create an observer for every token.
export function useReplyJump(root, session) {
  const [target, setTarget] = useState(null);
  useEffect(() => {
    setTarget(null);
    const viewport = root.current;
    const content = viewport?.querySelector(".conversation-content");
    if (!content) return;
    const observed = new Set(), visible = new Set();
    let frame = 0;
    function measure() {
      frame = 0;
      const view = viewport.getBoundingClientRect();
      const height = viewport.clientHeight;
      let best = null, overlap = height * 0.2;
      for (const reply of visible) {
        if (!reply.isConnected) continue;
        const box = reply.getBoundingClientRect();
        const shown = Math.min(box.bottom, view.bottom) - Math.max(box.top, view.top);
        if (box.height > height * 1.25 && box.top < view.top - height * 0.5 && shown > overlap) {
          best = reply;
          overlap = shown;
        }
      }
      setTarget(best);
    }
    function schedule() {
      frame ||= requestAnimationFrame(measure);
    }
    const intersections = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) visible.add(entry.target);
        else visible.delete(entry.target);
      }
      schedule();
    }, { root: viewport });
    function sync() {
      for (const reply of observed) {
        if (reply.parentElement === content) continue;
        intersections.unobserve(reply);
        observed.delete(reply);
        visible.delete(reply);
      }
      for (const reply of content.children) {
        if (!reply.matches(".message.assistant") || observed.has(reply)) continue;
        observed.add(reply);
        intersections.observe(reply);
      }
      schedule();
    }
    const mutations = new MutationObserver(sync);
    mutations.observe(content, { childList: true });
    const sizes = new ResizeObserver(schedule);
    sizes.observe(content);
    sizes.observe(viewport);
    viewport.addEventListener("scroll", schedule, { passive: true });
    sync();
    return () => {
      cancelAnimationFrame(frame);
      viewport.removeEventListener("scroll", schedule);
      mutations.disconnect();
      intersections.disconnect();
      sizes.disconnect();
    };
  }, [root, session]);
  return target;
}

export function scrollBehavior() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
}
