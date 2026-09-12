import { html, useEffect, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { Dialog } from "./dialogs.js";
import { count, duration, numberValue } from "./content.js";
import { reasoningNames } from "./models.js";
import { t, n, msg, formatNumber } from "./i18n.js";

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
    ...(data?.status === "interrupted" ? [[msg("Status"), t("Interrupted")]] : []),
    [msg("Model"), data?.model || t("Not reported")],
    [msg("Provider"), data?.provider || t("Not reported")],
    [
      msg("Reasoning setting"),
      reasoningNames[data?.reasoning] ? t(reasoningNames[data.reasoning]) :
        (data?.reasoning === "enabled" ? t("Enabled") : t("Not reported")),
    ],
    [
      msg("Reasoning text"),
      data?.has_reasoning || message.reasoning || message.reasoning_content
        ? t("Available")
        : t("Not shared"),
    ],
    [msg("Model call duration"), duration(data?.duration_seconds) || "—"],
    [
      msg("Finish reason"),
      data?.finish_reason?.replaceAll("_", " ") || t("Not reported"),
    ],
  ];
  return html`<${Dialog} title=${t("Response details")} className="usage-dialog" onClose=${onClose}>
    ${busy && html`<p class="field-help" role="status"><span class="spinner" /> ${t("Loading response details…")}</p>`}
    ${error && html`<p class="form-error" role="alert">${t(error)}</p>`}
    <dl class="usage-grid response-facts">${facts.map(
      ([label, value]) =>
        html`<div>
          <dt>${t(label)}</dt>
          <dd>${value}</dd>
        </div>`,
    )}</dl>
    ${
      data?.usage &&
      html`<div class="response-usage">
        <p class="detail-title">${t("Final model call")}</p>
        <dl class="usage-grid">
          ${[
            [msg("Input tokens"), data.usage.input_tokens],
            [msg("Output tokens"), data.usage.output_tokens],
            [msg("Cached input"), data.usage.cache_read_tokens],
            [msg("Reasoning tokens"), data.usage.reasoning_tokens],
          ].map(
            ([label, value]) =>
              html`<div>
                <dt>${t(label)}</dt>
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
  if (used === null) return null;
  const percent =
    maximum > 0 ? Math.min(100, (used / maximum) * 100) : null;
  const percentage = percent === null ? null : formatNumber(percent / 100, {
    style: "percent", maximumFractionDigits: 0,
  });
  const label = percent !== null
    ? t("{percent} context used · last request", { percent: percentage })
    : n("{count} input token · last request", "{count} input tokens · last request", used);
  return html`<span class="context-indicator" role="img" aria-label=${label} title=${label}>
    <${Icon} name="context" size=${18} /><span>${percentage ?? n("{count} token", "{count} tokens", used)}</span>
  </span>`;
}
