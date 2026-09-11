import { html, Icon, useEffect, useRef, useState } from "./lib.js";
import { api } from "./api.js";

const phases = {
  checking: "Checking for updates…",
  building: "Preparing update…",
  verifying: "Verifying update…",
  restarting: "Restarting Talaria…",
  recovering: "Restoring previous release…",
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

export function Installation() {
  const [info, setInfo] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [phase, setPhase] = useState("Checking for updates…");
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
        setPhase("Reconnecting to Talaria…");
        continue;
      }
      const job = data.operation;
      if (job?.id !== operation.id) {
        if (Date.now() > acceptanceDeadline)
          throw new Error("Could not confirm the update request. Check again before retrying.");
        continue;
      }
      setPhase(phases[job.phase] || "Updating Talaria…");
      if (job.status === "failed") throw new Error(job.error || "Update failed.");
      if (job.status === "completed") {
        if (operation.action === "update") {
          // Never reload against the old process merely because its socket responds.
          if (data.commit !== operation.expect) continue;
          location.reload();
        }
        return;
      }
    }
    if (!signal.aborted) throw new Error("The update is taking longer than expected. Check again shortly.");
  }

  async function submit(action, current, signal) {
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      (byte) => byte.toString(16).padStart(2, "0")).join("");
    const operation = { id, action };
    const body = { id: operation.id };
    if (action === "update") body.expect = operation.expect = current.update.latest_commit;
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
        else if (data.can_update) await submit("check", data, signal);
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
  }, []);

  const development = info?.environment === "development";
  const available = info?.update?.available;
  const problem = error || info?.update?.error;
  return html`<div class="section-heading">
      <h3>Talaria ${info && html`<span class="installation-version" aria-label=${`Version ${info.version}`}>${info.version}</span>`}</h3>
      ${development && html`<span class="environment-badge">Development</span>`}
    </div>
    ${info && html`<dl class="installation-details">
      <div><dt>Build</dt><dd>${development ? "Working checkout" : info.commit?.slice(0, 10) || "Installed package"}</dd></div>
      ${info.managed && html`<div><dt>Branch</dt><dd>${info.branch}</dd></div>`}
    </dl>`}
    ${!development && html`<section class="installation-update" aria-label="Updates">
      <p class="installation-status" role="status">${busy ? phase : problem ? "Update unavailable" : available
        ? "Update available" : info?.update?.checked_at ? "Up to date" : "Not checked"}</p>
      ${problem && !busy && html`<p class="form-error" role="alert">${problem}</p>`}
      ${info?.can_update ? html`<div class="installation-actions">
        <button class="button secondary" disabled=${busy} onClick=${() => act("check")}>
          <${Icon} name="refresh" size=${16} />Check for updates
        </button>
        ${available && html`<button class="button primary" disabled=${busy || !!problem}
          onClick=${() => act("update")}><${Icon} name="download" size=${16} />Update</button>`}
      </div>` : info && html`<p class="field-help">${info.managed
        ? "Start Talaria with its managed launcher to enable updates."
        : "Update using your package or container manager."}</p>`}
    </section>`}`;
}
