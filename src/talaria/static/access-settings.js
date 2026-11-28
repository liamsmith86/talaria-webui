import { html, useEffect, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { state, update, useStore } from "./store.js";
import { Installation, ReleaseSummary } from "./installation.js";
import { msg, rich, t } from "./i18n.js";

const releaseLabels = {
  current: msg("Up to date"),
  outdated: msg("Plugin update available"),
  newer: msg("Plugin is newer than this Talaria release"),
  different: msg("Plugin differs from this Talaria release"),
  unknown: msg("Plugin version not reported"),
};

const localSources = {
  catalog: msg("Hermes plugin catalog"),
  git: msg("Git, installed by Hermes"),
  other: msg("Installed by Hermes"),
  talaria: msg("Installed by Talaria"),
  manual: msg("Copied by hand"),
};

const localStatus = {
  missing: msg("Not installed"),
  outdated: msg("Update available · Latest: {bundled}"),
  unknown: msg("Update available · Latest: {bundled}"),
  current: releaseLabels.current,
  newer: releaseLabels.newer,
  different: releaseLabels.different,
};

// What the plugin files in the Hermes serving this connection need, as steps for their owner.
function localSteps(local, serving) {
  const stale = ["outdated", "unknown"].includes(local.status);
  if (local.status === "missing")
    return [[msg("To install it, run the following commands on Hermes Agent:"), local.commands.install]];
  if (local.managed === "hermes" && stale && local.source === "other")
    return [[msg("Hermes can't update this copy. To replace it with Talaria's copy, which Talaria keeps up to date, run the following commands on Hermes Agent:"), local.commands.switch]];
  if (local.managed === "hermes" && stale)
    return [[msg("To update the Talaria plugin, run the following commands on Hermes Agent:"), local.commands.hermes_update]];
  if (local.managed === "talaria" && local.status !== "current" && local.status !== "newer")
    return [[msg("Talaria keeps this plugin up to date. To update it now, use the button below."), null]];
  if (local.managed === "manual" && local.status !== "current" && local.status !== "newer")
    return [[msg("To update the Talaria plugin, run the following commands on Hermes Agent:"), local.commands.refresh]];
  if (!serving && local.status === "current")
    return [[msg("To enable it and restart Hermes, run the following commands on Hermes Agent:"), local.commands.enable]];
  return [];
}

// Shell commands for a narrow panel: long ones wrap, and one button copies them all.
function Commands({ commands }) {
  const [copied, setCopied] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(commands.join("\n"));
      setCopied("copied");
      setTimeout(() => setCopied(""), 1800);
    } catch {
      setCopied("failed");
    }
  }
  return html`<div class="commands">
    <div class="command-lines">${commands.map((command) => html`<code key=${command}>${command}</code>`)}</div>
    <button type="button" class="command-copy" onClick=${copy}>
      ${copied === "copied" ? t("Copied") : copied === "failed" ? t("Select text to copy") : t("Copy")}
    </button>
  </div>`;
}

// Laid out like Talaria's own release on its settings tab. Steps wait while Hermes itself is too
// old, since it would refuse the plugin.
function LocalPlugin({ local, serving, steps }) {
  const source = localSources[local.managed === "hermes" ? local.source : local.managed];
  return html`<div class="local-plugin">
    <${ReleaseSummary} level=${4} title=${t("Talaria plugin")} version=${local.version}
      details=${source ? [[t("Source"), t(source)]] : []} />
    <section class="installation-update" aria-label=${t("Updates")}>
      <p class="installation-status" role="status">
        ${t(localStatus[local.status], { bundled: local.bundled_version })}
      </p>
      ${steps && localSteps(local, serving).map(([text, commands]) => html`<div key=${text}>
        <p class="field-help">${t(text)}</p>
        ${commands && html`<${Commands} commands=${commands} />`}
      </div>`)}
    </section>
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
    ${available && !local && html`<p class="field-help" role="status">
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
          ? t("Hermes isn't loading the Talaria plugin")
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
      ${" "}${rich("To check whether it's installed and enabled, run {command} on that host.", { command: html`<code>hermes plugins show talaria-webui</code>` })}
    </p>`}
    ${checked && html`<p class="field-help" role="status">${unsupported
      ? t("Check complete. Talaria plugin can't run on this Hermes version.")
      : available
      ? t("Check complete. Talaria plugin is connected.")
      : outdated
      ? t("Check complete. Talaria plugin needs a newer Hermes.")
      : idle
      ? t("Check complete. Hermes isn't loading the Talaria plugin.")
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
