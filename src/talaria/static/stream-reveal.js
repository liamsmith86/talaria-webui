import { html, useLayoutEffect, useRef, useState } from "./lib.js";

const motionQuery = "(prefers-reduced-motion: reduce)";
const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

// Presentation only: transport, recovery receipts, and saved output always
// retain all received text. A terminal result bypasses the reveal queue.
export function useStreamReveal(text, active) {
  const [shown, setShown] = useState(text);
  const control = useRef({ shown: text, text, timer: 0, deadline: 0 });
  const state = control.current;
  function catchUp() {
    clearTimeout(state.timer);
    state.timer = 0;
    state.deadline = 0;
    state.shown = state.text;
    setShown(state.text);
  }
  function schedule() {
    if (state.timer || document.hidden || state.shown === state.text) return;
    state.deadline ||= performance.now() + 128;
    state.timer = setTimeout(() => {
      state.timer = 0;
      if (document.hidden) return;
      const backlog = state.text.slice(state.shown.length);
      // Drain bursts promptly; ordinary deltas get small, regular steps. No
      // per-frame animation loop and no artificial delay on a restored reply.
      const steps = Math.max(
        1,
        Math.ceil((state.deadline - performance.now()) / 32) + 1,
      );
      const wanted = Math.max(24, Math.ceil(backlog.length / steps));
      let size = backlog.length;
      for (const part of segmenter.segment(backlog)) {
        if (part.index >= wanted) {
          size = part.index;
          break;
        }
      }
      state.shown = state.text.slice(0, state.shown.length + size);
      if (state.shown === state.text) state.deadline = 0;
      setShown(state.shown);
      schedule();
    }, 32);
  }
  useLayoutEffect(() => {
    state.text = text;
    if (
      !active ||
      state.reduced ||
      !text.startsWith(state.shown) ||
      (!document.hidden && text.length - state.shown.length > 4096)
    )
      catchUp();
    else schedule();
  }, [text, active]);
  useLayoutEffect(() => {
    if (!active) return;
    const media = matchMedia(motionQuery);
    const visibility = () => {
      if (document.hidden) {
        clearTimeout(state.timer);
        state.timer = 0;
      } else catchUp();
    };
    const motion = () => {
      state.reduced = media.matches;
      if (media.matches) catchUp();
    };
    motion();
    document.addEventListener("visibilitychange", visibility);
    media.addEventListener("change", motion);
    return () => {
      clearTimeout(state.timer);
      document.removeEventListener("visibilitychange", visibility);
      media.removeEventListener("change", motion);
    };
  }, [active]);
  return !active || state.reduced || !text.startsWith(shown) ? text : shown;
}

// Small groups keep text-node identity and selections stable while limiting
// DOM growth to roughly one span per 64 characters, rather than per token.
// Opacity is advanced by the browser, independently of reveal timers.
export function StreamText({ text, active }) {
  const previous = useRef({ text: "", parts: [] });
  const animations = useRef(new Set());
  function finish() {
    for (const animation of animations.current) animation.cancel();
    animations.current.clear();
  }
  useLayoutEffect(() => {
    if (!active) {
      finish();
      return;
    }
    const media = matchMedia(motionQuery);
    const settle = () => {
      if (document.hidden || media.matches) finish();
    };
    document.addEventListener("visibilitychange", settle);
    media.addEventListener("change", settle);
    return () => {
      finish();
      document.removeEventListener("visibilitychange", settle);
      media.removeEventListener("change", settle);
    };
  }, [active]);
  const old = previous.current;
  if (!text.startsWith(old.text)) {
    finish();
    old.text = "";
    old.parts = [];
  }
  if (text !== old.text) {
    const delta = text.slice(old.text.length);
    const last = old.parts.at(-1);
    if (last && last.text.length < 64) {
      last.text += delta;
      last.view = null;
    } else {
      old.parts.push({
        text: delta,
        ref: (element) => {
          if (
            !element ||
            !active ||
            delta.length > 256 ||
            document.hidden ||
            matchMedia(motionQuery).matches
          )
            return;
          const animation = element.animate(
            [{ opacity: 0.4 }, { opacity: 1 }],
            {
              duration: 160,
              easing: "linear",
            },
          );
          animations.current.add(animation);
          animation.onfinish = () => {
            animations.current.delete(animation);
            animation.cancel();
          };
        },
      });
    }
    old.text = text;
  }
  return html`<span class="markdown-inline"
    >${old.parts.map(
      (part, i) =>
        (part.view ||= html`<span key=${i} ref=${part.ref}
          >${part.text}</span
        >`),
    )}</span
  >`;
}
