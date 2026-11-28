import { html, useEffect, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { state, update, useStore } from "./store.js";
import { Installation } from "./installation.js";
import { Markdown } from "./markdown.js";
import { msg, rich, t } from "./i18n.js";

const releaseLabels = {
  current: msg("Up to date"),
  outdated: msg("Plugin update available"),
  newer: msg("Plugin is newer than this Talaria release"),
  different: msg("Plugin differs from this Talaria release"),
  unknown: msg("Plugin version not reported"),
};

const localLabels = {
  catalog: msg("On this server: version {version}, installed from Hermes's plugin catalog."),
  hermes: msg("On this server: version {version}, installed by Hermes."),
  talaria: msg("On this server: version {version}, updated with Talaria."),
  manual: msg("On this server: version {version}, copied by hand and not updated automatically."),
};

// What the plugin files in the Hermes serving this connection need, as steps for their owner.
function localSteps(local, serving) {
  const stale = ["outdated", "unknown"].includes(local.status);
  if (local.status === "missing")
    return [[msg("Install it on this server, as {owner}:"), local.commands.install]];
  if (local.managed === "hermes" && stale && local.source === "other")
    return [[msg("Hermes can't update this copy. To use the plugin bundled with Talaria, which then updates with Talaria, run as {owner}:"), local.commands.switch]];
  if (local.managed === "hermes" && stale)
    return [
      [msg("Update it through Hermes, as {owner}:"), local.commands.hermes_update],
      ...(local.source === "catalog" ? [[msg("If Hermes finds no update, its catalog doesn't have version {bundled} yet. To use the plugin bundled with Talaria instead, which then updates with Talaria, run:"), local.commands.switch]] : []),
    ];
  if (local.managed === "talaria" && local.status !== "current" && local.status !== "newer")
    return [[msg("Talaria installs its bundled plugin with each update. The button below installs it now."), null]];
  if (local.managed === "manual" && local.status !== "current" && local.status !== "newer")
    return [[msg("Install the version bundled with Talaria, as {owner}:"), local.commands.refresh]];
  if (!serving && local.status === "current")
    return [[msg("Hermes isn't running the installed plugin. Enable it and restart the gateway, as {owner}:"), local.commands.enable]];
  return [];
}

// Steps wait while Hermes itself is too old, since it would refuse the plugin.
function LocalPlugin({ local, serving, steps }) {
  const label = local.managed === "hermes" ? localLabels[local.source] || localLabels.hermes : localLabels[local.managed];
  const values = { version: local.version || t("unknown"), owner: local.owner, bundled: local.bundled_version };
  return html`<div class="local-plugin">
    <p class="field-help" role="status">
      ${label ? t(label, values) : t("Not installed in the Hermes on this server.")}
    </p>
    ${steps && localSteps(local, serving).map(([text, commands]) => html`<div key=${text}>
      <p class="field-help">${t(text, values)}</p>
      ${commands && html`<${Markdown} text=${"```sh\n" + commands.join("\n") + "\n```"} />`}
    </div>`)}
  </div>`;
}

export function ExtendedAccess({ onSaved, readOnly = false }) {
  const app = useStore();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [checked, setChecked] = useState(false);
  const available = app.caps.talaria_extensions?.version === 1;
  const unsupported = available && app.caps.talaria_extensions.state === "unsupported_hermes";
  const restart = available && app.caps.talaria_extensions.state === "restart_required";
  const inherited = app.caps.talaria_extensions?.context_runs;
  const context = app.caps.talaria_extensions?.profile_context || {};
  const release = app.caps.talaria_extensions?.release;
  // Hermes refuses a plugin that requires a newer Hermes, so offer the Hermes update instead.
  const hermes = app.caps.talaria_hermes;
  const outdated = hermes?.supported === false;
  const needsInstall = !outdated && (!available || !["current", "newer"].includes(release?.status));
  const local = app.caps.talaria_local_plugin;
  // Installed on this machine, yet Hermes answers as if there were no plugin.
  const idle = !available && !outdated && local && local.status !== "missing";
  async function refresh(quiet = false) {
    setBusy(true);
    setError("");
    setChecked(false);
    try {
      const caps = await api("/capabilities");
      update({ caps, agent: caps.talaria_agent || state.agent });
      if (!quiet) {
        setChecked(true);
        onSaved?.();
      }
    } catch (e) {
      if (!quiet) setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  // What Talaria learned when the page loaded can predate a Hermes update or restart.
  useEffect(() => {
    refresh(true);
    // Once per opening; refresh only uses stable setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return html`<section class="extended-access">
    <div class="section-heading">
      <h3>${t("Extended access")}</h3>
      <span class="quiet-badge">${t("Optional")}</span>
    </div>
    ${available && html`<p class="field-help" role="status">
      ${release?.version && `${release.version} · `}${t(releaseLabels[release?.status] || releaseLabels.unknown)}
    </p>`}
    <div class="access-status" role="status">
      <${Icon} name=${available && !unsupported ? "check" : "info"} size=${16} /><span
        >${unsupported
          ? t("Talaria plugin can't run on this Hermes version")
          : available
          ? t("Connected · Talaria plugin")
          : outdated
          ? t("Talaria plugin needs a newer Hermes")
          : idle
          ? t("Talaria plugin installed but not running")
          : t("Talaria plugin not detected")}</span
      >
    </div>
    ${unsupported && !needsInstall && html`<p class="field-help">
      ${t("Check for a Talaria update that supports this Hermes version.")}
    </p>`}
    ${outdated && html`<p class="field-help">
      ${t("This Hermes is {version}. The Talaria plugin needs Hermes {minimum} or later: update Hermes, then check again.", hermes)}
    </p>`}
    ${restart && html`<p class="field-help" role="status">
      ${t("Restart the Hermes gateway after active work finishes to load the updated plugin.")}
    </p>`}
    ${available && inherited &&
      (context.instructions === "unavailable" || context.prefill === "unavailable") &&
      html`<p class="field-help" role="status">
        ${t("Some profile context could not be loaded. Check its configuration in Hermes, then check again.")}
      </p>`}
    ${local && html`<${LocalPlugin} local=${local} serving=${available && !unsupported}
      steps=${!outdated} />`}
    ${!available && !outdated && !local &&
    html`<p class="field-help">
      ${t("Install the plugin on your Hermes host, then restart its gateway.")}
      ${" "}${rich("If it's installed, {command} on that host shows its version and whether it's enabled.", { command: html`<code>hermes plugins show talaria-webui</code>` })}
    </p>`}
    ${checked && html`<p class="field-help" role="status">${unsupported
      ? t("Check complete. Talaria plugin can't run on this Hermes version.")
      : available
      ? t("Check complete. Talaria plugin is connected.")
      : outdated
      ? t("Check complete. Talaria plugin needs a newer Hermes.")
      : idle
      ? t("Check complete. Talaria plugin is installed but not running.")
      : t("Check complete. Talaria plugin was not detected.")}</p>`}
    ${error && html`<p class="form-error" role="alert">${t(error)}</p>`}
    <div class="dialog-actions">
      ${needsInstall && !local && html`<a class="button secondary"
        href="https://github.com/liamsmith86/talaria-webui#hermes-plugin" target="_blank" rel="noopener noreferrer">
        ${t("Server installation instructions")}
      </a>`}
      <button class="button secondary" disabled=${busy || readOnly} onClick=${() => refresh()}>
        ${busy ? t("Checking…") : t("Check again")}
      </button>
    </div>
    ${needsInstall && html`<${Installation} pluginOnly=${true} readOnly=${readOnly} />`}
  </section>`;
}
