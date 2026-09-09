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
import { api } from "./api.js";
import {
  state,
  update,
  connect,
  toast,
  fail,
  refreshSessions,
  newConversation,
  openSession,
} from "./store.js";

export function Dialog({ title, children, onClose, wide = false }) {
  const ref = useRef();
  const titleId = useRef(`dialog-${crypto.randomUUID()}`).current;
  useEffect(() => {
    const el = ref.current,
      previous = document.activeElement;
    el.showModal();
    return () => {
      el.close();
      previous?.focus();
    };
  }, []);
  return html`<dialog
    aria-labelledby=${titleId}
    class=${`dialog ${wide ? "wide" : ""}`}
    ref=${ref}
    onCancel=${(e) => {
      e.preventDefault();
      onClose();
    }}
    onClick=${(e) => {
      if (e.target === e.currentTarget) {
        const r = e.currentTarget.getBoundingClientRect();
        if (
          e.clientX < r.left ||
          e.clientX > r.right ||
          e.clientY < r.top ||
          e.clientY > r.bottom
        )
          onClose();
      }
    }}
  >
    <div class="dialog-heading">
      <h2 id=${titleId}>${title}</h2>
      <${IconButton} name="close" label="Close dialog" onClick=${onClose} />
    </div>
    ${children}
  </dialog>`;
}

export function Connection({ initial = false, onClose }) {
  const [url, setUrl] = useState("http://127.0.0.1:8642");
  const [key, setKey] = useState("");
  const [keySet, setKeySet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tested, setTested] = useState(false);
  useEffect(() => {
    api("/connection")
      .then((d) => {
        setUrl(d.url);
        setKeySet(d.key_set);
      })
      .catch((e) => setError(e.message));
  }, []);
  async function submit(save) {
    setBusy(true);
    setError("");
    setTested(false);
    try {
      await api(save ? "/connection" : "/connection/test", {
        method: save ? "PUT" : "POST",
        body: { url, api_key: key },
      });
      if (save) {
        await connect();
        onClose();
        toast("Connected to Hermes");
      } else setTested(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<${Dialog} title=${initial ? "Meet your Hermes" : "Connection"} onClose=${onClose}>
    <p class="dialog-intro">Connect to your Hermes Agent. Your conversations, tools, and memory stay with Hermes.</p>
    <form onSubmit=${(e) => {
      e.preventDefault();
      submit(true);
    }}>
      <label class="field">Hermes address<input type="url" value=${url} onInput=${(
        e,
      ) => {
        setUrl(e.target.value);
        setTested(false);
      }} required spellcheck="false" placeholder="http://127.0.0.1:8642"/></label>
      <label class="field">API key<input type="password" value=${key} onInput=${(
        e,
      ) => {
        setKey(e.target.value);
        setTested(false);
      }} placeholder=${keySet ? "Saved securely · leave blank to keep" : "Your Hermes API server key"} autocomplete="new-password"/></label>
      <p class="field-help">Your key is stored on the Talaria server and never sent back to your browser.</p>
      ${error && html`<div class="form-error" role="alert">${error}</div>`}
      ${tested && html`<div class="form-success" role="status"><${Icon} name="check" size=${16} /> Hermes is ready.</div>`}
      <div class="dialog-actions"><button type="button" class="button secondary" disabled=${busy} onClick=${() => submit(false)}>Test connection</button><button class="button primary" disabled=${busy}>${busy ? "Connecting…" : "Save connection"}</button></div>
    </form>
  </${Dialog}>`;
}

export function Settings({ onClose }) {
  const [theme, setTheme] = useState(readStorage("theme", "system"));
  const [palette, setPalette] = useState(readStorage("palette", "blue"));
  function appearance(key, value) {
    writeStorage(key, value);
    document.documentElement.dataset[key] = value;
    (key === "theme" ? setTheme : setPalette)(value);
  }
  return html`<${Dialog} title="Make yourself at home" onClose=${onClose}>
    <p class="dialog-intro">A few small things, just the way you like them.</p>
    <section class="setting-section"><h3>Appearance</h3><div class="appearance-options" role="group" aria-label="Appearance">
      ${[
        ["system", "monitor", "System"],
        ["light", "sun", "Light"],
        ["dark", "moon", "Dark"],
      ].map(
        ([id, icon, label]) =>
          html`<button
            class=${`appearance-option ${theme === id ? "selected" : ""}`}
            aria-pressed=${theme === id}
            onClick=${() => appearance("theme", id)}
          >
            <${Icon} name=${icon} />${label}
          </button>`,
      )}
    </div></section>
    <section class="setting-section"><h3>Accent color</h3><div class="palette-options" role="group" aria-label="Accent color">
      ${[
        ["blue", "Blue"],
        ["sage", "Sage"],
        ["violet", "Violet"],
        ["rose", "Rose"],
      ].map(
        ([id, label]) =>
          html`<button
            class=${`palette-option ${palette === id ? "selected" : ""}`}
            aria-pressed=${palette === id}
            onClick=${() => appearance("palette", id)}
          >
            <span class=${`swatch ${id}`}
              >${palette === id &&
              html`<${Icon} name="check" size=${16} />`}</span
            >${label}
          </button>`,
      )}
    </div></section>
    <section class="setting-section setting-links"><button onClick=${() => update({ modal: "connection" })}><span><${Icon} name="link"/>Hermes connection</span><${Icon} name="chevron"/></button>
      <button onClick=${async () => {
        try {
          await api("/logout", { method: "POST", body: {} });
          location.reload();
        } catch (e) {
          fail(e);
        }
      }}><span><${Icon} name="logout"/>Sign out</span></button>
    </section><div class="settings-footnote">Talaria <span>0.1.0 · Made for a little more flow.</span></div>
  </${Dialog}>`;
}

export function SessionDialog({ mode, session, onClose }) {
  const [title, setTitle] = useState(session.title || "Untitled conversation");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const base = `/sessions/${encodeURIComponent(session.id)}`;
      const result = await api(mode === "fork" ? `${base}/fork` : base, {
        method:
          mode === "delete" ? "DELETE" : mode === "fork" ? "POST" : "PATCH",
        body:
          mode === "delete"
            ? {}
            : {
                title:
                  mode === "fork" ? `${title} · branch`.slice(0, 160) : title,
              },
      });
      if (mode === "delete" && state.active === session.id) newConversation();
      await refreshSessions();
      onClose();
      if (mode === "fork")
        await openSession(result.id || result.session_id || result.session?.id);
      toast(
        mode === "delete"
          ? "Conversation deleted"
          : mode === "fork"
            ? "Conversation branched"
            : "Conversation renamed",
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<${Dialog} title=${{ rename: "Rename conversation", delete: "Delete conversation?", fork: "Branch conversation" }[mode]} onClose=${onClose}>
    <form onSubmit=${submit}>
      ${mode === "delete" ? html`<p class="dialog-intro">“${title}” will be permanently deleted from Hermes. This cannot be undone.</p>` : html`<label class="field">Conversation name<input value=${title} onInput=${(e) => setTitle(e.target.value)} maxlength="150" required autofocus /></label>`}
      ${mode === "fork" && html`<p class="field-help">Continue in a new direction with a copy of this conversation’s history.</p>`}
      ${error && html`<div class="form-error" role="alert">${error}</div>`}
      <div class="dialog-actions"><button type="button" class="button secondary" onClick=${onClose}>Cancel</button><button disabled=${busy} class=${`button ${mode === "delete" ? "danger" : "primary"}`}>${busy ? "Working…" : { rename: "Save name", delete: "Delete conversation", fork: "Create branch" }[mode]}</button></div>
    </form>
  </${Dialog}>`;
}

export function ModelPicker({ models, selected, onSelect, onClose }) {
  const [query, setQuery] = useState("");
  const filtered = models.filter((m) =>
    `${m.label} ${m.providerLabel} ${m.id}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return html`<${Dialog} title="Choose a model" onClose=${onClose}>
    <div class="search-field"><${Icon} name="search" size=${18}/><input aria-label="Search models" placeholder="Find a model…" value=${query} onInput=${(e) => setQuery(e.target.value)} autoFocus/></div>
    <div class="model-list"><button class="model-option" onClick=${() => {
      onSelect(null);
      onClose();
    }}><span>Hermes default<small>Use your agent’s configured model</small></span>${!selected && html`<${Icon} name="check" size=${18} />`}</button>
    ${filtered.map(
      (m) =>
        html`<button
          class="model-option"
          onClick=${() => {
            onSelect(m);
            onClose();
          }}
        >
          <span>${m.label}<small>${m.providerLabel}</small></span
          >${selected?.id === m.id &&
          selected?.provider === m.provider &&
          html`<${Icon} name="check" size=${18} />`}
        </button>`,
    )}
    ${!filtered.length && html`<p class="field-help">${query ? "No models match that search." : "Your Hermes default is available. Additional models appear when Hermes provides them."}</p>`}
    </div>
  </${Dialog}>`;
}
