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
          if (current) set({ data, busy: false, error: "" });
        })
        .catch((error) => {
          if (current)
            set({ data: initial, busy: false, error: error.message });
        });
    return () => {
      current = false;
    };
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
    },
  );
  const facts = [
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
    <p class="field-help usage-note">${
      data?.model
        ? "Recorded by Hermes for this response. Cached input and reasoning tokens may be included in the input and output totals."
        : "Hermes did not record model or reasoning settings for this response. Details become available for new responses with the Talaria plugin enabled."
    }</p>
  </${Dialog}>`;
}

export function ContextIndicator({ app }) {
  const [open, setOpen] = useState(false);
  const stamp = app.lives[app.active]?.status || "saved";
  const { data, busy, error } = useDetails(
    `/sessions/${encodeURIComponent(app.active)}/context?turn=${encodeURIComponent(stamp)}&head=${app.history.at(-1)?.id || 0}`,
    !!app.caps.talaria_extensions?.context_usage,
  );
  const context = data?.context;
  const used = numberValue(context?.used),
    maximum = numberValue(context?.maximum);
  const percent =
    used !== null && maximum > 0 ? Math.min(100, (used / maximum) * 100) : null;
  return html`<button
      class="context-button"
      aria-label="Context usage"
      title=${percent !== null
        ? `${Math.round(percent)}% context used · last request`
        : "Context usage"}
      aria-haspopup="dialog"
      onClick=${() => setOpen(true)}
    >
      <${Icon} name="context" size=${18} />${percent !== null &&
      html`<span>${Math.round(percent)}%</span>`}
    </button>
    ${open &&
    html`<${Dialog} title="Context usage" className="usage-dialog" onClose=${() => setOpen(false)}>
      ${busy && html`<p class="field-help" role="status"><span class="spinner" /> Loading context usage…</p>`}
      ${error && html`<p class="form-error" role="alert">${error}</p>`}
      ${
        context
          ? html` <div class="context-summary">
                <strong
                  >${percent !== null
                    ? `${Math.round(percent)}%`
                    : count(used)}</strong
                ><span
                  >${percent !== null
                    ? "used on the last request"
                    : "input tokens on the last request"}</span
                >
              </div>
              ${percent !== null &&
              html`<progress
                class="context-meter"
                max="100"
                value=${percent}
                aria-label="Context used on the last request"
              />`}
              <dl class="usage-grid">
                <div>
                  <dt>Used</dt>
                  <dd>${count(used)}</dd>
                </div>
                <div>
                  <dt>Remaining at that request</dt>
                  <dd>
                    ${count(
                      used !== null && maximum > 0
                        ? Math.max(0, maximum - used)
                        : null,
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Model context window</dt>
                  <dd>${count(maximum)}</dd>
                </div>
                <div>
                  <dt>Model</dt>
                  <dd>${context.model || "Not reported"}</dd>
                </div>
              </dl>
              <p class="field-help usage-note">
                Input tokens reported for the last model request. New replies,
                tool results, or Hermes’s automatic compression can change the
                next request’s usage. Remaining space also needs to accommodate
                the model’s output.
              </p>`
          : !busy &&
            !error &&
            html`<p class="dialog-intro">
              ${app.caps.talaria_extensions?.context_usage
                ? "No context measurement has been recorded for this conversation yet. It will appear after the next completed response."
                : "Enable the Talaria plugin in Hermes to see context usage for new responses."}
            </p>`
      }
    </${Dialog}>`}`;
}
