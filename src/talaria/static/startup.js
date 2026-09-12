import { html, Mark, useEffect, useRef, useState } from "./lib.js";
import { initialize } from "./store.js";

export function Startup() {
  const [failed, setFailed] = useState(false);
  const retry = useRef(() => {});
  useEffect(() => {
    let pending, timer, stopped = false;
    const visible = () => document.visibilityState === "visible" && navigator.onLine;
    async function start() {
      clearTimeout(timer);
      pending?.abort();
      if (!navigator.onLine) setFailed(true);
      if (stopped || !visible()) return;
      const controller = new AbortController();
      pending = controller;
      try {
        await initialize(controller.signal);
      } catch (_error) {
        if (controller.signal.aborted || stopped) return;
        setFailed(true);
        timer = setTimeout(start, 3000);
      }
    }
    retry.current = start;
    window.addEventListener("online", start);
    window.addEventListener("offline", start);
    window.addEventListener("focus", start);
    window.addEventListener("pageshow", start);
    document.addEventListener("visibilitychange", start);
    start();
    return () => {
      stopped = true;
      clearTimeout(timer);
      pending?.abort();
      window.removeEventListener("online", start);
      window.removeEventListener("offline", start);
      window.removeEventListener("focus", start);
      window.removeEventListener("pageshow", start);
      document.removeEventListener("visibilitychange", start);
    };
  }, []);
  return html`<div class="initial-loader">
    <${Mark} size=${40} />
    <span role="status">${failed ? "Reconnecting to Talaria…" : "Loading…"}</span>
    ${failed && html`<button class="button secondary" onClick=${() => retry.current()}>Retry connection</button>`}
  </div>`;
}
