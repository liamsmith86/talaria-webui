import { html, useState, Icon } from "./lib.js";
import { refreshReadiness } from "./store.js";
import { msg, t, formatNumber } from "./i18n.js";
import { numberValue, statusLabel } from "./content.js";

const issueLabels = {
  state_db: msg("Session storage"),
  session_store: msg("Session access"),
  config: msg("Configuration"),
  model: msg("Default model"),
  disk: msg("Storage space"),
  gateway: msg("Gateway"),
  background_queues: msg("Background work"),
};

function issueDetail(issue) {
  const used = numberValue(issue.used_percent);
  if (issue.name === "disk" && used !== null && used <= 100)
    return t("{used} of storage is used.", {
      used: formatNumber(used / 100, { style: "percent", maximumFractionDigits: 4 }),
    });
  return issue.detail ? t(issue.detail) : statusLabel(issue.status);
}

export function readinessLabel(app) {
  if (!app.connected) return t("Connection needed");
  if (app.readiness.status === "unavailable")
    return t("{name} unavailable", { name: app.agent.name });
  if (app.readiness.status === "degraded")
    return t("{name} needs attention", { name: app.agent.name });
  return t("Connected to {name}", { name: app.agent.name });
}
export function ReadinessNotice({ app, compact = false }) {
  const [busy, setBusy] = useState(false);
  const value = app.readiness;
  if (!app.connected || !["degraded", "unavailable"].includes(value.status))
    return null;
  return html`<section
    class=${`readiness-notice ${compact ? "compact" : ""}`}
    aria-label=${t("Hermes readiness")}
    role="status"
  >
    <${Icon} name="alert" size=${18} />
    <div>
      <strong>${readinessLabel(app)}</strong>
      ${value.message && html`<p>${t(value.message)}</p>`}
      ${value.issues?.length
        ? html`<ul>
            ${value.issues.map(
              (issue) =>
                html`<li>
                  <span>${issueLabels[issue.name] ? t(issueLabels[issue.name]) : issue.label}</span>${` · ${issueDetail(issue)}`}
                </li>`,
            )}
          </ul>`
        : !value.message &&
          html`<p>${t("Hermes reported a problem with its readiness checks.")}</p>`}
    </div>
    <button
      class="text-button"
      disabled=${busy}
      onClick=${async () => {
        setBusy(true);
        try {
          await refreshReadiness();
        } finally {
          setBusy(false);
        }
      }}
    >
      ${busy ? t("Checking…") : t("Check again")}
    </button>
  </section>`;
}
