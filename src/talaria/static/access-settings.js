import { html, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { state, update, useStore } from "./store.js";
import { Installation } from "./installation.js";

const installURL = "hermes://plugin/install?repo=liamsmith86%2Ftalaria-webui%2Fsrc%2Ftalaria%2Fhermes_plugin&enable=1";
const releaseLabels = {
  current: "Matches this Talaria release",
  outdated: "Plugin update available",
  newer: "Plugin is newer than this Talaria release",
  different: "Plugin differs from this Talaria release",
  unknown: "Plugin version not reported",
};

export function ExtendedAccess({ onSaved }) {
  const app = useStore();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const available = app.caps.talaria_extensions?.version === 1;
  const inherited = app.caps.talaria_extensions?.context_runs;
  const context = app.caps.talaria_extensions?.profile_context || {};
  const release = app.caps.talaria_extensions?.release;
  const needsInstall = !available || !["current", "newer"].includes(release?.status);
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
    ${available && html`<p class="field-help" role="status">
      ${release?.version && `${release.version} · `}${releaseLabels[release?.status] || releaseLabels.unknown}
    </p>`}
    <div class="access-status" role="status">
      <${Icon} name=${available ? "check" : "info"} size=${16} /><span
        >${available
          ? "Connected · Talaria plugin"
          : "Talaria plugin not detected"}</span
      >
    </div>
    ${available && inherited &&
      (context.instructions === "unavailable" || context.prefill === "unavailable") &&
      html`<p class="field-help" role="status">
        Some profile context could not be loaded. Check its configuration in Hermes,
        then check again.
      </p>`}
    ${!available &&
    html`<p class="field-help">
      Install the plugin on your Hermes host, then restart its gateway.
    </p>`}
    ${error && html`<p class="form-error" role="alert">${error}</p>`}
    <div class="dialog-actions">
      ${needsInstall && html`<a class="button secondary"
        href=${installURL + (available ? "&force=1" : "")}>
        ${available ? "Update in Hermes Desktop" : "Install in Hermes Desktop"}
      </a>`}
      <button class="button secondary" disabled=${busy} onClick=${refresh}>
        ${busy ? "Checking…" : "Check again"}
      </button>
    </div>
    ${needsInstall && html`<p class="field-help">
      <a href="https://github.com/liamsmith86/talaria-webui#hermes-plugin" target="_blank" rel="noopener noreferrer">Server installation instructions</a>
    </p>`}
    ${needsInstall && html`<${Installation} pluginOnly=${true} />`}
  </section>`;
}
