import {
  state, openSession, newConversation, refreshSessions, refreshHistory,
  refreshSessionDetails, navigationVersion, navigationSignal, fail, update,
} from "./store.js";
import { selectedSession, selectedMessage } from "./session-navigation.js";
import { running } from "./runs.js";

export function observePage() {
  let timer, pending, stopped = false, queued = false, last = -Infinity, reported = "";
  const visible = () => document.visibilityState === "visible" && navigator.onLine;
  async function refresh() {
    if (stopped || pending || !visible() || !state.auth || !state.connected ||
        state.loading || !state.caps.features?.session_resources) return;
    last = performance.now();
    const controller = new AbortController();
    pending = controller;
    queued = false;
    const id = state.active, generation = navigationVersion();
    const signal = navigationSignal();
    const abort = () => controller.abort();
    signal.addEventListener("abort", abort, { once: true });
    const current = () => !stopped && !controller.signal.aborted && generation === navigationVersion();
    const tasks = [refreshSessions(false, controller.signal)];
    // A local stream already owns its live-to-saved transition. Revalidation
    // catches up idle/external sessions without disturbing that transition.
    if (id && !state.searchWindow && !running(id)) {
      tasks.push(refreshHistory(id, current, null, controller.signal));
      tasks.push(refreshSessionDetails(id, controller.signal));
    }
    const results = await Promise.allSettled(tasks);
    if (current()) {
      const failed = results.find((result) => result.status === "rejected" && result.reason.name !== "AbortError");
      if (failed) {
        fail(failed.reason);
        reported = state.error;
      } else if (reported && state.error === reported) {
        update({ error: "" });
        reported = "";
      }
    }
    signal.removeEventListener("abort", abort);
    if (pending === controller) pending = null;
    if (queued && !stopped) schedule();
  }
  function schedule() {
    clearTimeout(timer);
    if (!visible()) {
      pending?.abort();
      return;
    }
    if (pending) {
      queued = true;
      return;
    }
    timer = setTimeout(refresh, Math.max(120, 5000 - (performance.now() - last)));
  }
  function navigate() {
    pending?.abort();
    if (!state.auth || !state.connected) return;
    const id = selectedSession();
    if (id) openSession(id, null, true, selectedMessage());
    else newConversation(true);
  }
  const restored = (event) => { if (event.persisted) schedule(); };
  window.addEventListener("popstate", navigate);
  window.addEventListener("pageshow", restored);
  window.addEventListener("focus", schedule);
  window.addEventListener("online", schedule);
  document.addEventListener("visibilitychange", schedule);
  return () => {
    stopped = true;
    clearTimeout(timer);
    pending?.abort();
    window.removeEventListener("popstate", navigate);
    window.removeEventListener("pageshow", restored);
    window.removeEventListener("focus", schedule);
    window.removeEventListener("online", schedule);
    document.removeEventListener("visibilitychange", schedule);
  };
}
