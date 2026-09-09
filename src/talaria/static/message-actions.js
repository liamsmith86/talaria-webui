import { html, useEffect, useState } from "./lib.js";
import { Dialog } from "./dialogs.js";
import { api } from "./api.js";
import {
  state,
  update,
  refreshHistory,
  refreshSessionDetails,
  refreshSessions,
  toast,
} from "./store.js";
import { running, sendMessage, clearCompletedRun } from "./runs.js";
import { sessionModel, sessionReasoning } from "./models.js";
import { plainContent } from "./content.js";
import {
  withCachedImages,
  messageImages,
  withoutImagePlaceholders,
} from "./attachments.js";
import { Images } from "./images.js";

export function MessageAction({ action, session, message, onClose }) {
  const [preview, setPreview] = useState(null);
  const [text, setText] = useState("");
  const [images, setImages] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [rewound, setRewound] = useState(false);
  const deleting = action === "delete",
    editing = action === "edit";
  const title = deleting
    ? "Delete turn"
    : editing
      ? "Edit and resend"
      : "Regenerate response";
  useEffect(() => {
    let current = true;
    api(`/sessions/${encodeURIComponent(session)}/rewind`, {
      method: "POST",
      body: { message_id: message.id, preview: true },
    })
      .then(async (value) => {
        const [user] = await withCachedImages(session, [value.user]);
        if (!current) return;
        setText(withoutImagePlaceholders(plainContent(user.content)));
        setImages(messageImages(user));
        setPreview(value);
      })
      .catch((e) => {
        if (current) setError(e.message);
      });
    return () => {
      current = false;
    };
  }, [session, message.id]);
  const missingImages =
    !deleting &&
    images.some(
      (image) => image.unavailable || !image.url?.startsWith("data:image/"),
    );
  async function confirm(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (session !== state.active || state.readOnlyParent || running(session))
        throw new Error(
          "The conversation is busy or has changed. Reopen this action when it finishes.",
        );
      if (!rewound) {
        await api(`/sessions/${encodeURIComponent(session)}/rewind`, {
          method: "POST",
          body: {
            message_id: message.id,
            revision: preview.revision,
            ...(!deleting
              ? {
                  replacement: {
                    input: text,
                    images: images.map(({ url }) => ({ url })),
                  },
                }
              : {}),
          },
        });
        setRewound(true);
        clearCompletedRun(session);
        await refreshHistory(session);
        refreshSessionDetails(session).catch(() => {});
        refreshSessions().catch(() => {});
      }
      if (!deleting)
        await sendMessage(text, sessionModel(state), {
          images,
          reasoning: sessionReasoning(state),
        });
      else toast("Turn removed from the conversation.");
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<${Dialog} title=${title} onClose=${onClose} dismissible=${!busy}>
    <form onSubmit=${confirm}>
      ${
        preview
          ? html`<p class="dialog-intro">
              ${rewound
                ? "The conversation has been rewound. Your message is ready to send."
                : `${deleting ? "Remove" : "Replace"} this turn${preview.turn_count > 1 ? ` and ${preview.turn_count - 1} later ${preview.turn_count === 2 ? "turn" : "turns"}` : ""}? Removed messages are kept in Hermes’s archived history. This does not undo actions already taken by tools.`}
            </p>`
          : !error &&
            html`<p class="dialog-intro" role="status">
              <span class="spinner" /> Checking the conversation…
            </p>`
      }
      ${editing && preview && html`<label class="field">Message<textarea class="edit-message-input" aria-label="Edit message" value=${text} maxlength="50000" rows="6" disabled=${busy} onInput=${(e) => setText(e.target.value)} /></label>`}
      ${editing && images.length > 0 && html`<${Images} images=${images} />`}
      ${!deleting && preview && html`<p class="field-help">Uses this session’s model and reasoning settings.</p>`}
      ${missingImages && html`<p class="form-error" role="alert">The original images are not available to resend. Your conversation has not been changed.</p>`}
      ${error && html`<p class="form-error" role="alert">${error}</p>`}
      <div class="dialog-actions"><button type="button" class="button secondary" disabled=${busy} onClick=${onClose}>${rewound ? "Close" : "Cancel"}</button>
        <button class=${`button ${deleting ? "danger" : "primary"}`} disabled=${busy || !preview || missingImages || (!deleting && !text.trim() && !images.length)}>${busy ? "Working…" : rewound && !deleting ? "Send message" : title}</button></div>
    </form>
  </${Dialog}>`;
}
