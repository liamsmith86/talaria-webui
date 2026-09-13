import { html, Icon, randomId, useEffect, useRef, useState } from "./lib.js";
import { api } from "./api.js";
import { msg, t } from "./i18n.js";

const CHECK_COOLDOWN = 10 * 60 * 1000;
let lastCheckAttempt = 0;

function recentlyChecked(timestamp) {
  const age = Date.now() - timestamp;
  return age >= 0 && age < CHECK_COOLDOWN;
}

const phases = {
  checking: msg("Checking for updates…"),
  building: msg("Preparing update…"),
  verifying: msg("Verifying update…"),
  restarting: msg("Restarting Talaria…"),
  plugin: msg("Updating local Hermes plugin…"),
  recovering: msg("Restoring previous release…"),
};

function delay(signal) {
  return new Promise((resolve, reject) => {
    const aborted = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", aborted);
      resolve();
    }, 1000);
    signal.addEventListener("abort", aborted, { once: true });
    if (signal.aborted) aborted();
  });
}

export function Installation({ pluginOnly = false, readOnly = false }) {
  const [info, setInfo] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [phase, setPhase] = useState(msg("Loading…"));
  const control = useRef(null);
  const acting = useRef(false);

  async function fetchInfo(signal) {
    const data = await api("/installation", {
      signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
    });
    setInfo(data);
    return data;
  }

  async function follow(operation, signal) {
    const deadline = Date.now() + 15 * 60 * 1000;
    const acceptanceDeadline = Date.now() + 15000;
    while (!signal.aborted && Date.now() < deadline) {
      await delay(signal);
      let data;
      try {
        data = await fetchInfo(signal);
      } catch (failure) {
        if (signal.aborted) throw failure;
        if (failure.status === 401) {
          location.reload();
          return;
        }
        setPhase(msg("Reconnecting to Talaria…"));
        continue;
      }
      const job = data.operation;
      if (job?.id !== operation.id) {
        if (Date.now() > acceptanceDeadline)
          throw new Error(msg("Could not confirm the update request. Check again before retrying."));
        continue;
      }
      setPhase(job.phase === "checking" && operation.action === "update"
        ? phases.building : phases[job.phase] || msg("Updating Talaria…"));
      if (job.status === "failed") throw new Error(job.error || msg("Update failed."));
      if (job.status === "completed") {
        if (operation.action === "update") {
          // Never reload against the old process merely because its socket responds.
          if (data.commit !== operation.expect) continue;
          location.reload();
        }
        return;
      }
    }
    if (!signal.aborted) throw new Error(msg("The update is taking longer than expected. Check again shortly."));
  }

  async function submit(action, current, signal) {
    const id = randomId();
    const operation = { id, action };
    const body = { id: operation.id };
    if (action === "update") body.expect = operation.expect = current.update.latest_commit;
    if (action === "check") lastCheckAttempt = Date.now();
    setPhase(action === "check" ? phases.checking : phases.building);
    try {
      const accepted = await api(`/installation/${action}`, {
        method: "POST", body,
        signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
      });
      Object.assign(operation, accepted);
    } catch (failure) {
      if (signal.aborted) throw failure;
      if (failure.status && failure.status < 500) throw failure;
      // A lost acknowledgement is ambiguous; observe the job instead of resubmitting.
    }
    await follow(operation, signal);
  }

  async function act(action) {
    if (acting.current) return;
    acting.current = true;
    setBusy(true);
    setError("");
    const signal = control.current.signal;
    try {
      await submit(action, info, signal);
    } catch (failure) {
      if (!signal.aborted) setError(failure.message);
    } finally {
      acting.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    control.current = controller;
    const { signal } = controller;
    acting.current = true;
    async function load() {
      try {
        const data = await fetchInfo(signal);
        if (["running", "finishing"].includes(data.operation?.status)) await follow(data.operation, signal);
        else if (!readOnly && data.can_update && (!pluginOnly || data.updates_local_plugin) &&
          !recentlyChecked(lastCheckAttempt) &&
          !recentlyChecked(Date.parse(data.update?.checked_at)))
          await submit("check", data, signal);
      } catch (failure) {
        if (!signal.aborted) setError(failure.message);
      } finally {
        acting.current = false;
        if (!signal.aborted) setBusy(false);
      }
    }
    load();
    return () => controller.abort();
    // These functions use only arguments, stable setters, and the lifetime's signal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pluginOnly, readOnly]);

  const development = info?.environment === "development";
  const available = info?.update?.available;
  const problem = error || info?.update?.error;
  const status = busy ? t(phase) : problem ? t("Update unavailable") : available
    ? t("Update available") : info?.update?.checked_at ? t("Up to date") : t("Not checked");
  if (pluginOnly && !info?.updates_local_plugin) return null;
  return html`${!pluginOnly && html`<div class="section-heading">
      <h3>Talaria ${info && html`<span class="installation-version" aria-label=${t("Version {version}", { version: info.version })}>${info.version}</span>`}</h3>
      ${development && html`<span class="environment-badge">${t("Development")}</span>`}
    </div>
    ${info && html`<dl class="installation-details">
      <div><dt>${t("Build")}</dt><dd>${development ? t("Working checkout") : info.commit?.slice(0, 10) || t("Installed package")}</dd></div>
      ${info.managed && html`<div><dt>${t("Branch")}</dt><dd>${info.branch}</dd></div>`}
    </dl>`}`}
    ${!development && html`<section class="installation-update" aria-label=${t("Updates")}>
      <p class="installation-status" role="status">${pluginOnly && !busy ? t("Talaria: {status}", { status }) : status}</p>
      ${problem && !busy && html`<p class="form-error" role="alert">${t(problem)}</p>`}
      ${info?.updates_local_plugin && html`<p class="field-help">${t("Linked local Hermes restarts if its plugin changes.")}</p>`}
      ${info?.can_update ? html`<div class="installation-actions">
        <button class="button secondary" disabled=${busy || readOnly} onClick=${() => act("check")}>
          <${Icon} name="refresh" size=${16} />${t("Check for updates")}
        </button>
        ${(available || info.updates_local_plugin) && html`<button class="button primary" disabled=${busy || readOnly || !!problem || !info.update?.latest_commit}
          onClick=${() => act("update")}><${Icon} name="download" size=${16} />${available ? pluginOnly ? t("Update Talaria & plugin") : t("Update") : t("Sync linked plugin")}</button>`}
      </div>` : info && html`<p class="field-help">${info.managed
        ? t("Start Talaria with its managed launcher to enable updates.")
        : t("Update using your package or container manager.")}</p>`}
    </section>`}`;
}
