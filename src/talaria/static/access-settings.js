import { html, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { state, update, useStore } from "./store.js";

export function ExtendedAccess({ onSaved }) {
  const app = useStore();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const available = app.caps.talaria_extensions?.version === 1;
  async function refresh() {
    setBusy(true);
    setError("");
    try {
      const caps = await api("/capabilities");
      update({ caps, agent: caps.talaria_agent || state.agent });
      onSaved?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<section class="extended-access">
    <div class="section-heading">
      <h3>Extended access</h3>
      <span class="quiet-badge">Optional</span>
    </div>
    <p class="dialog-intro">
      The Talaria plugin adds agent identity, response details, context usage,
      and conversation editing through your Hermes connection.
    </p>
    <div class="access-status" role="status">
      <${Icon} name=${available ? "check" : "info"} size=${16} /><span
        >${available
          ? "Connected · Talaria plugin"
          : "Talaria plugin not detected"}</span
      >
    </div>
    ${!available &&
    html`<p class="field-help">
      Install and enable the Talaria plugin in Hermes, then restart its gateway.
      Talaria will detect it automatically. Standard chat works without it.
    </p>`}
    ${error && html`<p class="form-error" role="alert">${error}</p>`}
    <div class="dialog-actions">
      <button class="button secondary" disabled=${busy} onClick=${refresh}>
        ${busy ? "Checking…" : "Check again"}
      </button>
    </div>
  </section>`;
}
