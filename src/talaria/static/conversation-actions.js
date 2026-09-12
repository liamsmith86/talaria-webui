import { html, useEffect, useRef, useState, Icon } from "./lib.js";
import { Dialog } from "./dialogs.js";
import { api } from "./api.js";
import { apiURL } from "./profile-context.js";
import { update, pinSession, supports } from "./store.js";
import { running } from "./runs.js";
import { count, money, numberValue, sourceLabel } from "./content.js";
import { browserAttachments } from "./attachments.js";
import { t, msg } from "./i18n.js";

export function ConversationMenu({ session, onClose, readOnly = false }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const actions = [
    ["details", "chart", msg("Session details")],
    ["download", "download", msg("Download transcript")],
    ["rename", "edit", msg("Rename")],
    ["fork", "branch", msg("Branch session")],
    ["delete", "trash", msg("Delete session")],
  ].filter(([type]) => !readOnly || ["details", "download"].includes(type));
  return html`<${Dialog} title=${session.title || t("Session")} onClose=${onClose} dismissible=${!busy}>
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
            ? t("Unpin session")
            : t("Pin session")}
        </button>`
      }
      ${actions.map(
        ([type, icon, label]) =>
          html`<button
            class=${type === "delete" ? "danger-text" : ""}
            disabled=${busy ||
            (type === "fork" && !supports("session_fork")) ||
            (type === "delete" && running(session.id))}
            onClick=${() => update({ modal: { type, session } })}
          >
            <${Icon} name=${icon} size=${19} />${t(label)}
          </button>`,
      )}
    </div>
    ${error && html`<div class="form-error" role="alert">${t(error)}</div>`}
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
            msg("Session details are not available from Hermes."),
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
    [msg("Input tokens"), details.input_tokens],
    [msg("Output tokens"), details.output_tokens],
    [msg("Cache read tokens"), details.cache_read_tokens],
    [msg("Cache write tokens"), details.cache_write_tokens],
    [msg("Reasoning tokens"), details.reasoning_tokens],
    [msg("Model calls"), details.api_call_count],
  ];
  return html`<${Dialog} title=${t("Session details")} className="usage-dialog" onClose=${onClose}>
    <p class="detail-title">${details.title || t("Untitled session")}${busy && html`<span class="details-refresh" role="status" aria-label=${t("Refreshing details")}><span class="spinner" /></span>`}</p>
    <div class="detail-context">${sourceLabel(details.source) && html`<span class="quiet-badge">${sourceLabel(details.source)}</span>`}${details.model && html`<span>${details.model}</span>`}</div>
    ${error && html`<div class="form-error" role="alert">${t(error)}</div>`}
    <section class="usage-total"><span>${actual ? t("Reported cost") : t("Estimated cost")}</span><strong>${money(cost)}</strong><small>${t("{currency} · this session", { currency: "USD" })}</small></section>
    <dl class="usage-grid">${counters.map(
      ([label, value]) =>
        html`<div>
          <dt>${t(label)}</dt>
          <dd>${count(value)}</dd>
        </div>`,
    )}</dl>
    ${actual && numberValue(details.estimated_cost_usd) !== null && html`<p class="field-help">${t("Hermes’s estimate: {cost}", { cost: money(details.estimated_cost_usd) })}</p>`}
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
        apiURL(
          `/sessions/${encodeURIComponent(session.id)}/export?format=${format}`,
        ),
        { signal: controller.current.signal },
      );
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(
          detail.error || msg("The transcript could not be downloaded. Try again."),
        );
      }
      const attachments =
        format === "json"
          ? await browserAttachments(session.id).catch(() => [])
          : [];
      let blob;
      if (attachments.length) {
        const transcript = await response.json();
        const ids = new Set(transcript.messages.map((message) => message.id));
        const retained = attachments.filter((item) => ids.has(item.message_id));
        if (retained.length) transcript.browser_attachments = retained;
        blob = new Blob([JSON.stringify(transcript, null, 2)], {
          type: "application/json",
        });
      } else blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      // eslint-disable-next-line no-control-regex -- strip control characters from filenames
      link.download = `${(session.title || t("Session")).replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 100)}.${format === "json" ? "json" : "md"}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      onClose();
    } catch (e) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      setBusy("");
    }
  }
  return html`<${Dialog} title=${t("Download transcript")} onClose=${onClose}>
    <div class="download-formats">
      <button disabled=${!!busy} onClick=${() => download("markdown")}><${Icon} name="file"/><span><strong>Markdown</strong><small>${t("Text transcript; image placeholders.")}</small></span><${Icon} name="download" size=${18}/></button>
      <button disabled=${!!busy} onClick=${() => download("json")}><${Icon} name="terminal"/><span><strong>JSON</strong><small>${t("Messages, tools, and image data.")}</small></span><${Icon} name="download" size=${18}/></button>
    </div>
    ${busy && html`<p class="download-progress" role="status"><span class="spinner" />${t("Preparing the complete transcript…")}</p>`}
    ${error && html`<div class="form-error" role="alert">${t(error)}</div>`}
  </${Dialog}>`;
}
