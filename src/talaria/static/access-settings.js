import { html, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { state, update, useStore } from "./store.js";
import { Installation } from "./installation.js";
import { msg, t } from "./i18n.js";

const releaseLabels = {
  current: msg("Matches this Talaria release"),
  outdated: msg("Plugin update available"),
  newer: msg("Plugin is newer than this Talaria release"),
  different: msg("Plugin differs from this Talaria release"),
  unknown: msg("Plugin version not reported"),
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
      <h3>${t("Extended access")}</h3>
      <span class="quiet-badge">${t("Optional")}</span>
    </div>
    ${available && html`<p class="field-help" role="status">
      ${release?.version && `${release.version} · `}${t(releaseLabels[release?.status] || releaseLabels.unknown)}
    </p>`}
    <div class="access-status" role="status">
      <${Icon} name=${available ? "check" : "info"} size=${16} /><span
        >${available
          ? t("Connected · Talaria plugin")
          : t("Talaria plugin not detected")}</span
      >
    </div>
    ${available && inherited &&
      (context.instructions === "unavailable" || context.prefill === "unavailable") &&
      html`<p class="field-help" role="status">
        ${t("Some profile context could not be loaded. Check its configuration in Hermes, then check again.")}
      </p>`}
    ${!available &&
    html`<p class="field-help">
      ${t("Install the plugin on your Hermes host, then restart its gateway.")}
    </p>`}
    ${error && html`<p class="form-error" role="alert">${t(error)}</p>`}
    <div class="dialog-actions">
      ${needsInstall && html`<a class="button secondary"
        href="https://github.com/liamsmith86/talaria-webui#hermes-plugin" target="_blank" rel="noopener noreferrer">
        ${t("Server installation instructions")}
      </a>`}
      <button class="button secondary" disabled=${busy} onClick=${refresh}>
        ${busy ? t("Checking…") : t("Check again")}
      </button>
    </div>
    ${needsInstall && html`<${Installation} pluginOnly=${true} />`}
  </section>`;
}
