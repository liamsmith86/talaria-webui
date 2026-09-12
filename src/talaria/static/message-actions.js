import { html, useEffect, useState } from "./lib.js";
import { Dialog } from "./dialogs.js";
import { api } from "./api.js";
import {
  state,
  navigationVersion,
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
import { msg, t, n } from "./i18n.js";

function rewindDescription(deleting, later) {
  if (later > 0) return deleting
    ? n(
      "Remove this turn and {count} later turn? Removed messages are kept in Hermes’s archived history. This does not undo actions already taken by tools.",
      "Remove this turn and {count} later turns? Removed messages are kept in Hermes’s archived history. This does not undo actions already taken by tools.",
      later,
    )
    : n(
      "Replace this turn and {count} later turn? Removed messages are kept in Hermes’s archived history. This does not undo actions already taken by tools.",
      "Replace this turn and {count} later turns? Removed messages are kept in Hermes’s archived history. This does not undo actions already taken by tools.",
      later,
    );
  return deleting
    ? t("Remove this turn? Removed messages are kept in Hermes’s archived history. This does not undo actions already taken by tools.")
    : t("Replace this turn? Removed messages are kept in Hermes’s archived history. This does not undo actions already taken by tools.");
}

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
    ? t("Delete turn")
    : editing
      ? t("Edit and resend")
      : t("Regenerate response");
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
    const generation = navigationVersion();
    setBusy(true);
    setError("");
    try {
      if (session !== state.active || state.readOnlyParent || running(session))
        throw new Error(
          msg("The session is busy or has changed. Reopen this action when it finishes."),
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
      if (session !== state.active || generation !== navigationVersion())
        throw new Error(
          msg("The session changed. Reopen it before sending this message."),
        );
      if (!deleting)
        await sendMessage(text, sessionModel(state), {
          images,
          reasoning: sessionReasoning(state),
        });
      else toast(msg("Turn removed from the session."));
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
                ? t("The session has been rewound. Your message is ready to send.")
                : rewindDescription(deleting, preview.turn_count - 1)}
            </p>`
          : !error &&
            html`<p class="dialog-intro" role="status">
              <span class="spinner" /> ${t("Checking the session…")}
            </p>`
      }
      ${editing && preview && html`<label class="field">${t("Message")}<textarea class="edit-message-input" aria-label=${t("Edit message")} value=${text} maxlength="50000" rows="6" disabled=${busy} onInput=${(e) => setText(e.target.value)} /></label>`}
      ${editing && images.length > 0 && html`<${Images} images=${images} />`}
      ${missingImages && html`<p class="form-error" role="alert">${t("The original images are not available to resend. Your session has not been changed.")}</p>`}
      ${error && html`<p class="form-error" role="alert">${t(error)}</p>`}
      <div class="dialog-actions"><button type="button" class="button secondary" disabled=${busy} onClick=${onClose}>${rewound ? t("Close") : t("Cancel")}</button>
        <button class=${`button ${deleting ? "danger" : "primary"}`} disabled=${busy || !preview || missingImages || (!deleting && !text.trim() && !images.length)}>${busy ? t("Working…") : rewound && !deleting ? t("Send message") : title}</button></div>
    </form>
  </${Dialog}>`;
}
