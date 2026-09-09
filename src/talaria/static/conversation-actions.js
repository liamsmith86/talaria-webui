import { html, useEffect, useRef, useState, Icon } from "./lib.js";
import { Dialog } from "./dialogs.js";
import { api } from "./api.js";
import { update, pinSession, supports } from "./store.js";
import { running } from "./runs.js";
import { count, money, numberValue, sourceLabel } from "./content.js";

export function ConversationMenu({ session, onClose, readOnly = false }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const actions = [
    ["details", "chart", "Conversation details"],
    ["download", "download", "Download transcript"],
    ["rename", "edit", "Rename"],
    ["fork", "branch", "Branch conversation"],
    ["delete", "trash", "Delete conversation"],
  ].filter(([type]) => !readOnly || ["details", "download"].includes(type));
  return html`<${Dialog} title=${session.title || "Conversation"} onClose=${onClose}>
    <div class="session-menu">
      ${
        !readOnly &&
        html`<button
          disabled=${busy}
          onClick=${async () => {
            setBusy(true);
            try {
              await pinSession(session);
              onClose();
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <${Icon} name="pin" size=${19} />${session.pinned
            ? "Unpin conversation"
            : "Pin conversation"}
        </button>`
      }
      ${actions.map(
        ([type, icon, label]) =>
          html`<button
            class=${type === "delete" ? "danger-text" : ""}
            disabled=${(type === "fork" && !supports("session_fork")) ||
            (type === "delete" && running(session.id))}
            onClick=${() => update({ modal: { type, session } })}
          >
            <${Icon} name=${icon} size=${19} />${label}
          </button>`,
      )}
    </div>
    ${error && html`<div class="form-error" role="alert">${error}</div>`}
  </${Dialog}>`;
}

export function ConversationDetails({ session, onClose }) {
  const [details, setDetails] = useState(session);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    let current = true;
    api(`/sessions/${encodeURIComponent(session.id)}`)
      .then((value) => {
        const next = value?.session || value;
        if (!next || typeof next !== "object" || Array.isArray(next))
          throw new Error(
            "Conversation details are not available from Hermes.",
          );
        if (current) setDetails(next);
      })
      .catch((e) => {
        if (current) setError(e.message);
      })
      .finally(() => {
        if (current) setBusy(false);
      });
    return () => {
      current = false;
    };
  }, [session.id]);
  const cost =
    numberValue(details.actual_cost_usd) !== null
      ? details.actual_cost_usd
      : details.estimated_cost_usd;
  const actual = numberValue(details.actual_cost_usd) !== null;
  const counters = [
    ["Input tokens", details.input_tokens],
    ["Output tokens", details.output_tokens],
    ["Cache read tokens", details.cache_read_tokens],
    ["Cache write tokens", details.cache_write_tokens],
    ["Reasoning tokens", details.reasoning_tokens],
    ["Model calls", details.api_call_count],
  ];
  return html`<${Dialog} title="Conversation details" className="usage-dialog" onClose=${onClose}>
    <p class="detail-title">${details.title || "Untitled conversation"}${busy && html`<span class="details-refresh" role="status" aria-label="Refreshing details"><span class="spinner" /></span>`}</p>
    <div class="detail-context">${sourceLabel(details.source) && html`<span class="quiet-badge">${sourceLabel(details.source)}</span>`}${details.model && html`<span>${details.model}</span>`}</div>
    ${error && html`<div class="form-error" role="alert">${error}</div>`}
    <section class="usage-total"><span>${actual ? "Reported cost" : "Estimated cost"}</span><strong>${money(cost)}</strong><small>USD · this conversation</small></section>
    <dl class="usage-grid">${counters.map(
      ([label, value]) =>
        html`<div>
          <dt>${label}</dt>
          <dd>${count(value)}</dd>
        </div>`,
    )}</dl>
    ${actual && numberValue(details.estimated_cost_usd) !== null && html`<p class="field-help">Hermes’s estimate: ${money(details.estimated_cost_usd)}</p>`}
    <p class="field-help usage-note">Conversation totals reported by Hermes. A dash means the value was not shared. Cache and reasoning counts may be included in input or output totals.</p>
  </${Dialog}>`;
}

export function TranscriptDownload({ session, onClose }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const controller = useRef();
  useEffect(() => () => controller.current?.abort(), []);
  async function download(format) {
    setError("");
    setBusy(format);
    controller.current = new AbortController();
    try {
      const response = await fetch(
        `/api/sessions/${encodeURIComponent(session.id)}/export?format=${format}`,
        { signal: controller.current.signal },
      );
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(
          detail.error || "The transcript could not be downloaded. Try again.",
        );
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `${(session.title || "Conversation").replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 100)}.${format === "json" ? "json" : "md"}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      onClose();
    } catch (e) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      setBusy("");
    }
  }
  return html`<${Dialog} title="Download transcript" onClose=${onClose}>
    <p class="dialog-intro">Includes all saved messages, including earlier history. A response still in progress appears once Hermes saves it.</p>
    <div class="download-formats">
      <button disabled=${!!busy} onClick=${() => download("markdown")}><${Icon} name="file"/><span><strong>Markdown</strong><small>A readable transcript. Images are noted in the text.</small></span><${Icon} name="download" size=${18}/></button>
      <button disabled=${!!busy} onClick=${() => download("json")}><${Icon} name="terminal"/><span><strong>JSON</strong><small>Original message structure, tools, and image content.</small></span><${Icon} name="download" size=${18}/></button>
    </div>
    ${busy && html`<p class="download-progress" role="status"><span class="spinner" />Preparing the complete transcript…</p>`}
    ${error && html`<div class="form-error" role="alert">${error}</div>`}
  </${Dialog}>`;
}
