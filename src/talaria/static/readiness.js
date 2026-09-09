import { html, useState, Icon } from "./lib.js";
import { refreshReadiness } from "./store.js";

export function readinessLabel(app) {
  if (!app.connected) return "Connection needed";
  if (app.readiness.status === "unavailable")
    return `${app.agent.name} unavailable`;
  if (app.readiness.status === "degraded")
    return `${app.agent.name} needs attention`;
  return `Connected to ${app.agent.name}`;
}
export function ReadinessNotice({ app, compact = false }) {
  const [busy, setBusy] = useState(false);
  const value = app.readiness;
  if (!app.connected || !["degraded", "unavailable"].includes(value.status))
    return null;
  return html`<section
    class=${`readiness-notice ${compact ? "compact" : ""}`}
    aria-label="Hermes readiness"
    role="status"
  >
    <${Icon} name="alert" size=${18} />
    <div>
      <strong>${readinessLabel(app)}</strong>
      ${value.message && html`<p>${value.message}</p>`}
      ${value.issues?.length
        ? html`<ul>
            ${value.issues.map(
              (issue) =>
                html`<li>
                  <span>${issue.label}</span>${issue.detail
                    ? ` · ${issue.detail}`
                    : ` · ${issue.status.replace(/_/g, " ")}`}
                </li>`,
            )}
          </ul>`
        : !value.message &&
          html`<p>Hermes reported a problem with its readiness checks.</p>`}
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
      ${busy ? "Checking…" : "Check again"}
    </button>
  </section>`;
}
