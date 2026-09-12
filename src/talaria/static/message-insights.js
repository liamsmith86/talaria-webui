import { html, useEffect, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { Dialog } from "./dialogs.js";
import { count, duration, numberValue } from "./content.js";
import { reasoningNames } from "./models.js";

function useDetails(path, enabled, initial = null) {
  const [result, set] = useState({ data: initial, busy: enabled, error: "" });
  useEffect(() => {
    let current = true;
    set({ data: initial, busy: enabled, error: "" });
    if (enabled)
      api(path)
        .then((data) => {
          if (current)
            set({ data: { ...initial, ...data }, busy: false, error: "" });
        })
        .catch((error) => {
          if (current)
            set({ data: initial, busy: false, error: error.message });
        });
    return () => {
      current = false;
    };
    // Initial details seed this request; object identity changes on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, enabled]);
  return result;
}

export function ResponseDetails({ session, message, enabled, onClose }) {
  const { data, busy, error } = useDetails(
    `/sessions/${encodeURIComponent(session)}/response?message_id=${message.id}`,
    enabled,
    {
      timestamp: message.timestamp,
      finish_reason: message.finish_reason,
      has_reasoning: !!(message.reasoning || message.reasoning_content),
      status:
        message.responseStatus === "cancelled"
          ? "interrupted"
          : message.responseStatus,
    },
  );
  const facts = [
    ...(data?.status === "interrupted" ? [["Status", "Interrupted"]] : []),
    ["Model", data?.model || "Not reported"],
    ["Provider", data?.provider || "Not reported"],
    [
      "Reasoning setting",
      reasoningNames[data?.reasoning] ||
        (data?.reasoning === "enabled" ? "Enabled" : "Not reported"),
    ],
    [
      "Reasoning text",
      data?.has_reasoning || message.reasoning || message.reasoning_content
        ? "Available"
        : "Not shared",
    ],
    ["Model call duration", duration(data?.duration_seconds) || "—"],
    [
      "Finish reason",
      data?.finish_reason?.replaceAll("_", " ") || "Not reported",
    ],
  ];
  return html`<${Dialog} title="Response details" className="usage-dialog" onClose=${onClose}>
    ${busy && html`<p class="field-help" role="status"><span class="spinner" /> Loading response details…</p>`}
    ${error && html`<p class="form-error" role="alert">${error}</p>`}
    <dl class="usage-grid response-facts">${facts.map(
      ([label, value]) =>
        html`<div>
          <dt>${label}</dt>
          <dd>${value}</dd>
        </div>`,
    )}</dl>
    ${
      data?.usage &&
      html`<div class="response-usage">
        <p class="detail-title">Final model call</p>
        <dl class="usage-grid">
          ${[
            ["Input tokens", data.usage.input_tokens],
            ["Output tokens", data.usage.output_tokens],
            ["Cached input", data.usage.cache_read_tokens],
            ["Reasoning tokens", data.usage.reasoning_tokens],
          ].map(
            ([label, value]) =>
              html`<div>
                <dt>${label}</dt>
                <dd>${count(value)}</dd>
              </div>`,
          )}
        </dl>
      </div>`
    }
  </${Dialog}>`;
}

export function ContextIndicator({ app }) {
  const stamp = app.lives[app.active]?.status || "saved";
  const { data } = useDetails(
    `/sessions/${encodeURIComponent(app.active)}/context?turn=${encodeURIComponent(stamp)}&head=${app.history.at(-1)?.id || 0}`,
    !!app.caps.talaria_extensions?.context_usage,
  );
  const context = data?.context;
  const used = numberValue(context?.used),
    maximum = numberValue(context?.maximum);
  const percent =
    used !== null && maximum > 0 ? Math.min(100, (used / maximum) * 100) : null;
  const label = percent !== null
    ? `${Math.round(percent)}% context used · last request`
    : used !== null ? `${count(used)} input tokens · last request` : "Context usage unavailable";
  return html`<span class="context-indicator" role="img" aria-label=${label} title=${label}>
    <${Icon} name="context" size=${18} />${used !== null &&
    html`<span>${percent !== null ? `${Math.round(percent)}%` : `${count(used)} tokens`}</span>`}
  </span>`;
}
