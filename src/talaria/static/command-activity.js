import { html, useEffect, useRef, useState, Icon, IconButton, readStorage, writeStorage } from "./lib.js";
import { api } from "./api.js";
import { state, openSession, refreshHistory, refreshSessionDetails, refreshSessions, navigationVersion } from "./store.js";

function savedCommand() {
  try {
    const value = JSON.parse(readStorage("command.pending", "null"));
    return value && typeof value.id === "string" && typeof value.session === "string" ? value : null;
  } catch {
    return null;
  }
}

function forgetCommand(id) {
  if (savedCommand()?.id === id) writeStorage("command.pending", "null");
}

export const commandRunning = (activity) =>
  ["starting", "running", "reconnecting"].includes(activity?.status);

// Owned by the page, so opening a read-only child or changing sessions cannot
// unmount recovery. The activity is presentation only, never a Hermes message.
export function useCommandActivity(app) {
  const authenticated = app.auth === true;
  const [pending, setPending] = useState(savedCommand);
  const [feedback, setFeedback] = useState(null);
  const [waiting, setWaiting] = useState(false);
  const starting = useRef(false);
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  useEffect(() => {
    if (!pending || !authenticated) return;
    let current = true;
    let timer;
    const generation = navigationVersion();
    async function poll() {
      try {
        const result = await api(`/commands/${encodeURIComponent(pending.id)}`);
        if (!current) return;
        setWaiting(false);
        if (result.status === "running") {
          timer = setTimeout(poll, 1000);
          return;
        }
        let afterId = pending.afterId ?? null;
        if (result.status === "completed" && result.changed === true && result.session_id) {
          refreshSessions().catch(() => {});
          afterId = null;
          if (state.active === pending.session && navigationVersion() === generation) {
            if (result.session_id === pending.session) {
              await refreshHistory(pending.session);
              refreshSessionDetails(pending.session).catch(() => {});
            } else await openSession(pending.session);
            if (!current) return;
            if (state.active === result.session_id && !state.loading)
              afterId = state.history.at(-1)?.id || 0;
          }
        }
        setFeedback({ ...pending, session: result.session_id || pending.session, afterId,
          status: result.status, changed: result.changed, text: result.text || result.error });
        setPending(null);
        forgetCommand(pending.id);
      } catch (error) {
        if (!current) return;
        if (error.status >= 400 && error.status < 500) {
          setFeedback({ ...pending, status: "failed",
            text: "Command result unavailable. Refresh the session before retrying." });
          setPending(null);
          forgetCommand(pending.id);
        } else {
          setWaiting(true);
          timer = setTimeout(poll, 5000);
        }
      }
    }
    poll();
    return () => { current = false; clearTimeout(timer); };
  }, [pending, authenticated]);

  useEffect(() => {
    if (feedback && feedback.afterId == null && feedback.session === (app.active || "") && !app.loading)
      setFeedback({ ...feedback, afterId: app.history.at(-1)?.id || 0 });
  }, [feedback, app.active, app.history, app.loading]);

  async function start(command, args, session, text) {
    if (starting.current || pendingRef.current) throw new Error("Wait for the current command to finish.");
    const job = { id: crypto.randomUUID(), session, command, args, started: Date.now(), afterId: state.history.at(-1)?.id || 0 };
    starting.current = true;
    setWaiting(false);
    setFeedback({ ...job, status: "starting" });
    // Persist before dispatch. Ambiguous responses recover by GET, never a second POST.
    writeStorage("command.pending", JSON.stringify(job));
    const draftKey = `draft.${session || "new"}`;
    writeStorage(draftKey, "");
    try {
      await api("/commands", { method: "POST", body: {
        request_id: job.id, session_id: session, command, args,
      } });
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        forgetCommand(job.id);
        if (!readStorage(draftKey)) writeStorage(draftKey, text);
        setFeedback(null);
        throw error;
      }
    } finally {
      starting.current = false;
    }
    pendingRef.current = job;
    setPending(job);
  }
  return {
    start,
    show: setFeedback,
    dismiss: () => setFeedback(null),
    current: pending?.session === (app.active || "")
      ? { ...pending, status: waiting ? "reconnecting" : "running" } : feedback,
  };
}

function activityHeading(activity) {
  if (activity.status === "reconnecting") return "Reconnecting to Hermes…";
  if (activity.status === "failed") return "Command failed";
  if (commandRunning(activity)) return activity.command === "compress" ? "Compressing context…" : "Running command…";
  if (activity.command === "compress") {
    if (/--(?:preview|dry-run|dryrun)\b/.test(activity.args || "")) return "Compression preview";
    return activity.changed ? "Context compressed" : "Compression result";
  }
  return "Command result";
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
  const elapsed = seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
  const failed = activity.status === "failed";
  return html`<section class=${`command-card${busy ? " is-running" : failed ? " is-failed" : ""}`}
    aria-label=${`/${activity.command} command`}>
    <div class="command-card-topline">
      <span class="command-card-label"><${Icon} name="terminal" size=${14} />/${activity.command}</span>
      ${busy ? html`<span class="command-elapsed" aria-label=${`Elapsed ${elapsed}`}>${elapsed}</span>` :
        html`<${IconButton} name="close" label="Dismiss command result" onClick=${onDismiss} />`}
    </div>
    <div class="command-card-result" role="status" aria-live="polite" aria-atomic="true">
      <div class="command-card-heading">
        <span class="command-card-icon">${busy ? html`<span class="spinner" />` :
          html`<${Icon} name=${failed ? "alert" : "check"} size=${18} />`}</span>
        <strong>${activityHeading(activity)}</strong>
      </div>
      ${!busy && activity.text && html`<pre class="command-output">${activity.text}</pre>`}
    </div>
  </section>`;
}
