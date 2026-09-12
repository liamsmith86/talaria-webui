import { useEffect, useState } from "./lib.js";
import { api } from "./api.js";
import { t } from "./i18n.js";

const statuses = new Set(["queued", "running", "waiting_for_approval", "waiting_for_input", "completed", "failed", "cancelled", "interrupted"]);
export function activityStatus(live, remote) {
  const newer = live && remote && remote.run_id !== live.id && Number.isFinite(live.createdAt) &&
    Number.isFinite(remote.created_at) && remote.created_at * 1000 > (live?.createdAt || 0);
  const run = newer ? remote : live || remote;
  if (!run || run.uncertain || run.outcomeUnknown) return null;
  if (["waiting_for_approval", "waiting_for_input"].includes(run.status)) return "needs_input";
  if (["starting", "queued", "running"].includes(run.status)) return "running";
  if (run.status === "completed") return "finished";
  if (run.status === "failed") return "failed";
  if (["cancelled", "interrupted"].includes(run.status)) return "stopped";
  return null;
}

export function activityLabel(live, remote) {
  const status = activityStatus(live, remote);
  if (status === "needs_input") return t("Needs input");
  if (status === "running") return t("Running");
  if (status === "finished") return t("Finished");
  if (status === "failed") return t("Failed");
  if (status === "stopped") return t("Stopped");
  return null;
}

export function useSessionActivity(enabled) {
  const [activity, setActivity] = useState({});
  useEffect(() => {
    if (!enabled) { setActivity({}); return; }
    let timer, pending, stopped = false, last = -Infinity;
    const visible = () => document.visibilityState === "visible" && navigator.onLine;
    async function refresh() {
      if (stopped || pending || !visible()) return;
      const controller = new AbortController();
      pending = controller;
      last = performance.now();
      try {
        const result = await api("/activity", { signal: controller.signal });
        if (!controller.signal.aborted) setActivity(Object.fromEntries(result.data
          .filter((run) => typeof run?.session_id === "string" && statuses.has(run.status))
          .map((run) => [run.session_id, run])));
      } catch {
        if (!controller.signal.aborted) setActivity({});
      } finally {
        if (pending === controller) pending = null;
        if (!stopped && visible()) timer = setTimeout(refresh, 15000);
      }
    }
    function wake() {
      clearTimeout(timer);
      if (!visible()) { pending?.abort(); return; }
      timer = setTimeout(refresh, Math.max(0, 5000 - (performance.now() - last)));
    }
    wake();
    window.addEventListener("focus", wake);
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      stopped = true; clearTimeout(timer); pending?.abort();
      window.removeEventListener("focus", wake);
      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [enabled]);
  return activity;
}
