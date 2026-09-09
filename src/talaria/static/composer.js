import {
  html,
  useEffect,
  useRef,
  useState,
  Icon,
  IconButton,
  shortModel,
  readStorage,
  writeStorage,
} from "./lib.js";
import { sendMessage, stopRun, running } from "./runs.js";
import { fail, supports, toast } from "./store.js";

export function Composer({ app, model, onModel, draftSuggestion }) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const textarea = useRef();
  const input = useRef();
  const active = running(app.active, app.lives);
  const live = app.lives[app.active];
  const key = `draft.${app.active || "new"}`;
  const currentKey = useRef(key);
  currentKey.current = key;
  useEffect(() => {
    setDraft(readStorage(key));
  }, [key]);
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
    if (sending || !draft.trim()) return;
    const text = draft.trim();
    setSending(true);
    try {
      const sid = await sendMessage(text, model);
      if (currentKey.current === key || currentKey.current === `draft.${sid}`) {
        setDraft("");
        textarea.current?.focus();
      }
      writeStorage(key, "");
    } catch (error) {
      fail(error);
    } finally {
      setSending(false);
    }
  }
  async function attach(file) {
    if (!file) return;
    if (
      file.size > 200000 ||
      !(
        /\.(txt|md|py|js|ts|json|csv|yaml|yml|html|css|log)$/i.test(
          file.name,
        ) || file.type.startsWith("text/")
      )
    ) {
      toast("Attach a text or code file up to 200 KB.");
      return;
    }
    const text = await file.text();
    const next = `${draft}${draft ? "\n\n" : ""}File: ${file.name}\n\n\`\`\`\n${text}\n\`\`\``;
    setDraft(next);
    writeStorage(key, next);
    textarea.current?.focus();
  }
  return html`<form class="composer" onSubmit=${send}>
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
        disabled=${!app.connected || sending}
        maxlength="800000"
      />
    </div>
    <div class="composer-toolbar">
      <div class="composer-left">
        <${IconButton}
          name="plus"
          label="Attach a text file"
          disabled=${!app.connected || active}
          onClick=${() => input.current?.click()}
        /><input
          ref=${input}
          type="file"
          class="sr-only"
          tabindex="-1"
          aria-label="Text attachment"
          accept="text/*,.md,.py,.js,.ts,.json,.csv,.yaml,.yml"
          onChange=${(e) => {
            attach(e.target.files[0]).catch(fail);
            e.target.value = "";
          }}
        /><span class="toolbar-divider" /><button
          class="model-button"
          type="button"
          onClick=${onModel}
          disabled=${active || !app.connected}
        >
          <span>${model ? shortModel(model.id) : "Hermes default"}</span
          ><${Icon} name="chevron" size=${14} />
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
              disabled=${!draft.trim() ||
              sending ||
              !app.connected ||
              !supports("run_submission") ||
              (active && !supports("run_steer"))}
            >
              <${Icon} name="arrow" size=${19} />
            </button>`}
      </div>
    </div>
  </form>`;
}
