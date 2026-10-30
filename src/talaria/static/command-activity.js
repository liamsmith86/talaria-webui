import { html, useEffect, useRef, useState, Icon, IconButton, randomId, readStorage, writeStorage } from "./lib.js";
import { api } from "./api.js";
import {
  state, beforeHistoryUpdate, dismissCommand, refreshCommands, refreshHistory, refreshSessionDetails,
  refreshSessions, restoreDraft, showCommand, toast,
} from "./store.js";
import { duration } from "./content.js";
import { commandDescription } from "./commands.js";
import { msg, t } from "./i18n.js";

export const commandRunning = (activity) =>
  ["starting", "running", "reconnecting"].includes(activity?.status);

const preview = (args) => /--(?:preview|dry-run|dryrun)\b/.test(args || "");
// A compaction that changed the transcript is part of the conversation, shown
// on every device. Any other result stays until someone dismisses it.
export const compacted = (record) => record?.command === "compress" &&
  record.status === "completed" && record.changed === true && !preview(record.args);

// Hermes's time for the newest saved row. A result keeps this place in history:
// in-place compaction gives kept rows new IDs, but never new timestamps.
function historyStamp(history) {
  let latest = null;
  for (const row of history) {
    const stamp = Number(row?.timestamp);
    if (Number.isFinite(stamp) && (latest === null || stamp > latest)) latest = stamp;
  }
  return latest;
}

const NONE = [];

function fromRecord(record, clock, reconnecting) {
  const running = record.status === "running";
  return {
    id: record.id,
    session: record.session_id,
    command: record.command,
    args: record.args,
    status: running ? (reconnecting ? "reconnecting" : "running") : record.status,
    changed: record.changed === true,
    text: record.text,
    // Talaria's own explanation, translated where it is shown.
    notice: record.status === "unknown"
      ? msg("Hermes did not report this command's result. Check the conversation before running it again.")
      : null,
    // Hermes's time for the newest row when it began, or else Talaria's.
    anchor: Number.isFinite(record.anchor) ? record.anchor : record.started_at,
    // Server time, in this browser's clock.
    started: record.started_at * 1000 - clock,
    finished: record.finished_at,
    compaction: compacted(record),
    shared: true,
  };
}

// Repeat step(key) a second after each success, or five after a failure,
// while key is set and until step returns false. Whether the last one failed.
function useRepeat(key, step) {
  const [failing, setFailing] = useState(false);
  const latest = useRef(step);
  latest.current = step;
  useEffect(() => {
    setFailing(false);
    if (!key) return;
    let stopped = false, timer;
    async function attempt() {
      let failed = false, done = false;
      try {
        done = (await latest.current(key)) === false;
      } catch {
        failed = true;
      }
      if (stopped) return;
      setFailing(failed);
      if (!done) timer = setTimeout(attempt, failed ? 5000 : 1000);
    }
    timer = setTimeout(attempt, 1000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [key]);
  return failing;
}

// Talaria's server records every native command and follows it to its end,
// so each device, tab, and reload shows the same activity. Browser state holds
// only the moment before the server answers, /help, and commands run outside
// a session.
export function useCommandActivity(app) {
  const enabled = app.auth === true && app.caps.talaria_extensions?.commands === true;
  const [local, setLocal] = useState(null);
  const localRef = useRef(local);
  localRef.current = local;
  const starting = useRef(false);
  const seen = useRef({ session: null, statuses: new Map() });
  const loaded = enabled && app.active && app.commands.session === app.active ? app.active : null;
  const shared = loaded ? app.commands.records : NONE;
  const running = shared.some((record) => record.status === "running");

  // Revalidate with the transcript: opening a session, focus, and each turn.
  useEffect(() => {
    if (!enabled) return;
    let queued = false;
    const refresh = () => {
      if (queued) return;
      queued = true;
      queueMicrotask(() => {
        queued = false;
        refreshCommands().catch(() => {});
      });
    };
    refresh();
    return beforeHistoryUpdate(refresh);
  }, [enabled]);

  const reconnecting = useRepeat(running && app.active, () => refreshCommands());

  // Once listed, a session's record replaces this tab's placeholder.
  useEffect(() => {
    if (local?.session && shared.some((record) => record.id === local.id)) setLocal(null);
  }, [shared, local]);

  // A compaction that finishes while a session is open, here or on another
  // device, shows its new transcript. Opening a session loads it already.
  useEffect(() => {
    const previous = seen.current;
    seen.current = { session: loaded, statuses: new Map(shared.map((record) => [record.id, record.status])) };
    if (!loaded || previous.session !== loaded || !shared.some((record) => record.status === "completed" &&
        record.changed && previous.statuses.get(record.id) !== "completed")) return;
    refreshSessions().catch(() => {});
    refreshHistory(loaded).catch(() => {});
    refreshSessionDetails(loaded).catch(() => {});
  }, [shared, loaded]);

  // Commands outside a session have no shared list, and a start whose answer
  // was lost is not known to be listed. Follow these by ID.
  const polled = enabled && local && (!local.session || local.unconfirmed) && local.status !== "starting" &&
    commandRunning(local) ? local.id : null;
  const lost = useRepeat(polled, async (id) => {
    let record;
    try {
      record = await api(`/commands/${encodeURIComponent(id)}`);
    } catch (error) {
      const current = localRef.current;
      if (error.status !== 404 && error.code !== "state_unavailable") throw error;
      if (current?.id !== id) return false;
      if (current.unconfirmed && error.status === 404) {
        // Talaria never recorded it, so it never ran.
        setLocal(null);
        toast(restoreDraft(current.session, current.draft)
          ? msg("This command did not start. It is back in the message box.")
          : msg("This command did not start."));
      } else {
        setLocal({ ...current, status: "failed", notice: error.status === 404
          ? msg("Command result unavailable. Refresh the session before retrying.") : error.message });
      }
      return false;
    }
    if (record.session_id) {
      setLocal((current) => current?.id === id ? { ...current, unconfirmed: false } : current);
      showCommand(record);
      return false;
    }
    setLocal((current) => current?.id !== id ? current : record.status === "running"
      ? { ...current, status: "running" }
      : { ...fromRecord(record, 0, false), started: current.started, shared: false });
    return record.status === "running";
  });

  const activities = shared.map((record) => fromRecord(record, app.commands.clock, reconnecting));
  if (local && local.session === (app.active || "") && !shared.some((record) => record.id === local.id))
    activities.push(commandRunning(local) && (local.unconfirmed || lost)
      ? { ...local, status: "reconnecting" } : local);
  const busy = activities.some(commandRunning);

  async function start(command, args, session, text) {
    if (starting.current || busy) throw new Error(msg("Wait for the current command to finish."));
    const id = randomId();
    starting.current = true;
    setLocal({ id, session, command, args, draft: text, status: "starting", started: Date.now(),
      anchor: historyStamp(state.history), shared: false });
    // Sent: a reload must not offer it again.
    const draftKey = `draft.${session || "new"}`;
    writeStorage(draftKey, "");
    let record;
    try {
      record = await api("/commands", { method: "POST", body: {
        request_id: id, session_id: session, command, args,
      } });
    } catch (error) {
      if ((error.status >= 400 && error.status < 500) || error.code === "state_unavailable") {
        // Refused before anything ran. It never left the message box; keep it saved there.
        if (!readStorage(draftKey).trim()) writeStorage(draftKey, text);
        setLocal(null);
        throw error;
      }
      // A lost answer may still have started the command. Follow it by ID,
      // never post twice, and never offer it to run again while that is unknown.
      setLocal((current) => current?.id === id ? { ...current, status: "running", unconfirmed: true } : current);
      return;
    } finally {
      starting.current = false;
    }
    if (session) showCommand(record);
    else setLocal({ ...fromRecord(record, 0, false), started: Date.now(), shared: false });
  }
  function show(activity) {
    setLocal({ ...activity, anchor: historyStamp(state.history), shared: false });
  }
  function dismiss(activity) {
    if (activity.shared) dismissCommand(activity.id);
    else setLocal(null);
  }
  return { start, show, dismiss, activities, busy };
}

function activityHeading(activity) {
  if (activity.status === "reconnecting") return t("Reconnecting to Hermes…");
  if (activity.status === "failed") return t("Command failed");
  if (activity.status === "unknown") return t("Command result unknown");
  if (commandRunning(activity)) return activity.command === "compress" ? t("Compressing context…") : t("Running command…");
  if (activity.command === "compress") {
    if (preview(activity.args)) return t("Compression preview");
    return activity.changed ? t("Context compressed") : t("Compression result");
  }
  return t("Command result");
}

export function CommandCard({ activity, onDismiss }) {
  const busy = commandRunning(activity);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [busy]);
  const seconds = Math.max(0, Math.floor((now - (activity.started || now)) / 1000));
  const elapsed = duration(seconds);
  const failed = ["failed", "unknown"].includes(activity.status);
  const output = Array.isArray(activity.commands)
    ? activity.commands.map((command) =>
      `/${command.name} — ${commandDescription(command)}`).join("\n")
    : activity.notice ? t(activity.notice) : activity.text;
  return html`<section class=${`command-card${busy ? " is-running" : failed ? " is-failed" : ""}`}
    aria-label=${t("/{command} command", { command: activity.command })}>
    <div class="command-card-topline">
      <span class="command-card-label"><${Icon} name="terminal" size=${14} />/${activity.command}</span>
      ${busy ? html`<span class="command-elapsed" aria-label=${t("Elapsed {duration}", { duration: elapsed })}>${elapsed}</span>` :
        html`<${IconButton} name="close" label=${t("Dismiss command result")} onClick=${onDismiss} />`}
    </div>
    <div class="command-card-result" role="status" aria-live="polite" aria-atomic="true">
      <div class="command-card-heading">
        <span class="command-card-icon">${busy ? html`<span class="spinner" />` :
          html`<${Icon} name=${failed ? "alert" : "check"} size=${18} />`}</span>
        <strong>${activityHeading(activity)}</strong>
      </div>
      ${!busy && output && html`<pre class="command-output">${output}</pre>`}
    </div>
  </section>`;
}
