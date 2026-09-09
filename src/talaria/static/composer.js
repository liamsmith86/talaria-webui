import {
  html,
  useEffect,
  useRef,
  useState,
  Icon,
  IconButton,
  readStorage,
  writeStorage,
} from "./lib.js";
import { sendMessage, stopRun, running } from "./runs.js";
import { fail, supports, navigationVersion } from "./store.js";
import {
  prepareImage,
  pendingStorage,
  MAX_IMAGES_TOTAL,
} from "./attachments.js";
import { sessionReasoning, reasoningNames } from "./models.js";

export function Composer({ app, model, onModel, draftSuggestion }) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [images, setImages] = useState([]);
  const [attaching, setAttaching] = useState(false);
  const [loadingImages, setLoadingImages] = useState(true);
  const [attachmentError, setAttachmentError] = useState("");
  const [dragging, setDragging] = useState(false);
  const operation = useRef(false);
  const submission = useRef(false);
  const textarea = useRef();
  const input = useRef();
  const active = running(app.active, app.lives);
  const live = app.lives[app.active];
  const displayedModel = model || app.defaultModel;
  const reasoning = sessionReasoning(app);
  const key = `draft.${app.active || "new"}`;
  const currentKey = useRef(key);
  const currentDraft = useRef(draft);
  currentKey.current = key;
  currentDraft.current = draft;
  useEffect(() => {
    setDraft(readStorage(key));
    setImages([]);
    setLoadingImages(true);
    setAttachmentError("");
    let current = true;
    pendingStorage(key)
      .then((saved) => {
        if (current && Array.isArray(saved)) setImages(saved.slice(0, 4));
      })
      .catch(() => {})
      .finally(() => {
        if (current) setLoadingImages(false);
      });
    return () => {
      current = false;
    };
  }, [key]);
  useEffect(() => {
    if (live?.recovered && live.userText === readStorage(key)) {
      setDraft("");
      setImages([]);
      writeStorage(key, "");
      pendingStorage(key, null).catch(() => {});
    }
  }, [live?.recovered, key]);
  useEffect(() => {
    if (draftSuggestion) {
      setDraft(draftSuggestion.text);
      textarea.current?.focus();
    }
  }, [draftSuggestion]);
  useEffect(() => {
    const el = textarea.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [draft]);
  async function send(e) {
    e?.preventDefault();
    if (
      submission.current ||
      attaching ||
      loadingImages ||
      live?.uncertain ||
      (!draft.trim() && !images.length)
    )
      return;
    const text = draft.trim();
    const generation = navigationVersion();
    submission.current = true;
    setSending(true);
    try {
      const sid = await sendMessage(text, model, { images, reasoning });
      if (
        generation === navigationVersion() &&
        (currentKey.current === key || currentKey.current === `draft.${sid}`)
      ) {
        setDraft("");
        setImages([]);
        textarea.current?.focus();
      }
      writeStorage(`draft.${sid}`, "");
      pendingStorage(`draft.${sid}`, null).catch(() => {});
      if (generation === navigationVersion()) {
        writeStorage(key, "");
        pendingStorage(key, null).catch(() => {});
      }
    } catch (error) {
      fail(error);
    } finally {
      setSending(false);
      submission.current = false;
    }
  }
  async function attach(files) {
    if (!files.length || operation.current || submission.current) return;
    if (active) {
      setAttachmentError(
        "Attachments can be sent after this response finishes.",
      );
      return;
    }
    operation.current = true;
    setAttaching(true);
    setAttachmentError("");
    try {
      const savedImages = loadingImages ? await pendingStorage(key) : images;
      const nextImages = Array.isArray(savedImages) ? [...savedImages] : [];
      let appendedText = "";
      for (const file of files) {
        if (file.type.startsWith("image/")) {
          if (nextImages.length >= 4)
            throw new Error("You can attach up to four images per message.");
          nextImages.push(await prepareImage(file));
          if (
            nextImages.reduce((n, image) => n + image.size, 0) >
            MAX_IMAGES_TOTAL
          )
            throw new Error(
              "Images must total no more than 6 MB. Remove an image or choose smaller files.",
            );
        } else {
          if (
            file.size > 200000 ||
            !(
              file.type.startsWith("text/") ||
              /\.(txt|md|py|js|ts|json|csv|yaml|yml|html|css|log)$/i.test(
                file.name,
              )
            )
          )
            throw new Error(
              "Attach an image, or a text/code file up to 200 KB.",
            );
          const text = await file.text();
          appendedText += `${appendedText ? "\n\n" : ""}File: ${file.name}\n\n\`\`\`\n${text}\n\`\`\``;
          if (appendedText.length > 800000)
            throw new Error(
              "The message is too long. Remove some text before attaching another file.",
            );
        }
      }
      if (nextImages.length) await pendingStorage(key, nextImages);
      const latestText =
        currentKey.current === key
          ? currentDraft.current
          : readStorage(key, draft);
      const nextText =
        latestText +
        (appendedText ? `${latestText ? "\n\n" : ""}${appendedText}` : "");
      if (nextText.length > 800000)
        throw new Error(
          "The message is too long. Remove some text before attaching another file.",
        );
      if (appendedText) writeStorage(key, nextText);
      if (currentKey.current === key) {
        setImages(nextImages);
        if (appendedText) setDraft(nextText);
        textarea.current?.focus();
      }
    } catch (e) {
      if (currentKey.current === key) setAttachmentError(e.message);
    } finally {
      operation.current = false;
      setAttaching(false);
    }
  }
  return html`<form
    class=${`composer ${dragging ? "drop-active" : ""}`}
    onSubmit=${send}
    onDragOver=${(e) => {
      if ([...e.dataTransfer.types].includes("Files")) {
        e.preventDefault();
        setDragging(true);
      }
    }}
    onDragLeave=${(e) => {
      if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false);
    }}
    onDrop=${(e) => {
      e.preventDefault();
      setDragging(false);
      attach([...e.dataTransfer.files]);
    }}
  >
    ${images.length > 0 &&
    html`<div class="attachment-tray" aria-label="Image attachments">
      ${images.map(
        (image) =>
          html`<div class="attachment-chip" key=${image.id}>
            <img src=${image.url} alt=${image.name} /><span
              >${image.name}<small
                >${image.resized ? "Resized · " : ""}${Math.max(
                  1,
                  Math.round(image.size / 1024),
                )}
                KB</small
              ></span
            >
            <${IconButton}
              name="close"
              label=${`Remove ${image.name}`}
              disabled=${sending || attaching}
              onClick=${async () => {
                const next = images.filter((item) => item.id !== image.id);
                try {
                  await pendingStorage(key, next.length ? next : null);
                  if (currentKey.current === key) setImages(next);
                } catch (e) {
                  setAttachmentError(e.message);
                }
              }}
            />
          </div>`,
      )}
    </div>`}
    ${attaching &&
    html`<p class="attachment-progress" role="status">
      Preparing attachments…
    </p>`}
    ${attachmentError &&
    html`<div class="attachment-error" role="alert">
      ${attachmentError}<${IconButton}
        name="close"
        label="Dismiss attachment error"
        onClick=${() => setAttachmentError("")}
      />
    </div>`}
    <div class="composer-input">
      <textarea
        ref=${textarea}
        aria-label="Message Hermes"
        placeholder=${active
          ? "Guide Hermes while it works…"
          : "Where shall we begin?"}
        value=${draft}
        rows="1"
        onInput=${(e) => {
          currentDraft.current = e.target.value;
          setDraft(e.target.value);
          writeStorage(key, e.target.value);
        }}
        onKeyDown=${(e) => {
          if (
            e.key === "Enter" &&
            !e.shiftKey &&
            !e.isComposing &&
            !matchMedia("(pointer: coarse)").matches
          ) {
            e.preventDefault();
            send();
          }
        }}
        onPaste=${(e) => {
          const files = [...(e.clipboardData?.files || [])];
          if (files.length) {
            e.preventDefault();
            attach(files);
          }
        }}
        disabled=${!app.connected || sending}
        maxlength="800000"
      />
    </div>
    <div class="composer-toolbar">
      <div class="composer-left">
        <${IconButton}
          name="plus"
          label="Attach files"
          disabled=${!app.connected ||
          active ||
          attaching ||
          sending ||
          loadingImages}
          onClick=${() => input.current?.click()}
        /><input
          ref=${input}
          type="file"
          class="sr-only"
          tabindex="-1"
          aria-label="File attachment"
          multiple
          accept="image/png,image/jpeg,image/webp,image/gif,text/*,.md,.py,.js,.ts,.json,.csv,.yaml,.yml"
          onChange=${(e) => {
            attach([...e.target.files]);
            e.target.value = "";
          }}
        /><span class="toolbar-divider" /><button
          class="model-button"
          type="button"
          aria-label="Choose model"
          title=${displayedModel
            ? `${displayedModel.providerLabel} · ${displayedModel.id}${!model ? " · Default" : ""}`
            : "Use the default model configured in Hermes"}
          onClick=${onModel}
          disabled=${active || !app.connected}
        >
          <span class="model-caption"
            ><small>${displayedModel?.providerLabel || "Hermes"}</small
            ><span>${displayedModel?.id || "Configured model"}</span></span
          >
          ${!model && html`<span class="default-badge">Default</span>`}
          ${reasoning !== "auto" &&
          html`<span class="reasoning-badge" title="Reasoning effort"
            >${reasoningNames[reasoning]}</span
          >`}
          <${Icon} name="chevron" size=${14} />
        </button>
      </div>
      <div class="composer-right">
        ${active &&
        html`<span class="working-label"
          ><span class="live-dot" />${live.status === "stopping"
            ? "Stopping"
            : live.approval
              ? "Awaiting approval"
              : "Working"}</span
        >`}
        ${active && !draft.trim()
          ? html`<button
              type="button"
              class="send-button stop-button"
              aria-label="Stop response"
              title="Stop response"
              disabled=${!supports("run_stop") ||
              live.status === "stopping" ||
              !live.id}
              onClick=${() => stopRun(app.active).catch(fail)}
            >
              <${Icon} name="stop" size=${17} />
            </button>`
          : html`<button
              class="send-button"
              type="submit"
              aria-label=${active ? "Send guidance" : "Send message"}
              title=${active ? "Send guidance" : "Send message"}
              disabled=${(!draft.trim() && !images.length) ||
              sending ||
              attaching ||
              loadingImages ||
              (active && images.length > 0) ||
              !app.connected ||
              !supports("run_submission") ||
              !supports("session_resources") ||
              live?.uncertain ||
              (active && !supports("run_steer"))}
            >
              <${Icon} name="arrow" size=${19} />
            </button>`}
      </div>
    </div>
  </form>`;
}
