import { t } from "./i18n.js";

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
