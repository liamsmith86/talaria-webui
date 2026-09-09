import {
  html,
  useEffect,
  useRef,
  useState,
  Icon,
  IconButton,
  writeStorage,
} from "./lib.js";
import { api } from "./api.js";
import { pendingStorage, forgetImages } from "./attachments.js";
import {
  state,
  update,
  connect,
  toast,
  fail,
  refreshSessions,
  newConversation,
  openSession,
  chooseModel,
  chooseReasoning,
} from "./store.js";
import {
  sessionModel,
  sessionReasoning,
  reasoningNames,
  reasoningOptions,
} from "./models.js";

export function Dialog({
  title,
  children,
  onClose,
  wide = false,
  className = "",
  dismissible = true,
}) {
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
    class=${`dialog ${wide ? "wide" : ""} ${className}`}
    ref=${ref}
    onCancel=${(e) => {
      e.preventDefault();
      if (dismissible) onClose();
    }}
    onClick=${(e) => {
      if (dismissible && e.target === e.currentTarget) {
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
      ${dismissible &&
      html`<${IconButton}
        name="close"
        label="Close dialog"
        onClick=${onClose}
      />`}
    </div>
    ${children}
  </dialog>`;
}

export function Connection({
  initial = false,
  onClose,
  embedded = false,
  creating = false,
}) {
  const [url, setUrl] = useState("http://127.0.0.1:8642");
  const [profile, setProfile] = useState("default");
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const [keySet, setKeySet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tested, setTested] = useState(false);
  useEffect(() => {
    if (creating) {
      setUrl(state.profile?.server_url || state.profiles[0]?.server_url || url);
      return;
    }
    api("/connection")
      .then((d) => {
        setUrl(d.server_url || d.url);
        setProfile(d.profile || "default");
        setKeySet(d.key_set);
      })
      .catch((e) => setError(e.message));
  }, []);
  async function submit(save) {
    setBusy(true);
    setError("");
    setTested(false);
    try {
      await api(
        save
          ? creating
            ? "/profiles"
            : "/connection"
          : creating
            ? "/profiles/test"
            : "/connection/test",
        {
          method: save && !creating ? "PUT" : "POST",
          body: { url, profile, label, api_key: key },
        },
      );
      if (save) {
        if (!creating) await connect();
        onClose();
        toast(creating ? "Profile added" : `Connected to ${state.agent.name}`);
      } else setTested(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const content = html`
    <p class="dialog-intro">
      ${creating
        ? "Use the profile’s name and API key from Hermes. Its conversations, tools, and memory stay with Hermes."
        : "This connection belongs to the selected profile. Add another profile to connect elsewhere."}
    </p>
    <form
      onSubmit=${(e) => {
        e.preventDefault();
        submit(true);
      }}
    >
      ${creating &&
      html`<label class="field"
        >Display name<input
          value=${label}
          onInput=${(e) => setLabel(e.target.value)}
          maxlength="80"
          required
          placeholder="Research"
      /></label>`}
      <label class="field"
        >Hermes address<input
          type="url"
          value=${url}
          readonly=${keySet && !creating}
          onInput=${(e) => {
            setUrl(e.target.value);
            setTested(false);
          }}
          required
          spellcheck="false"
          placeholder="http://127.0.0.1:8642"
      /></label>
      <label class="field"
        >Hermes profile<input
          value=${profile}
          onInput=${(e) => {
            setProfile(e.target.value);
            setTested(false);
          }}
          readonly=${keySet && !creating}
          required
          spellcheck="false"
          maxlength="64"
          placeholder="default"
      /></label>
      <p class="field-help">
        Use <code>default</code> for the main agent, or the name of an existing
        Hermes profile. Named profiles need their own API key.
      </p>
      <label class="field"
        >API key<input
          type="password"
          value=${key}
          onInput=${(e) => {
            setKey(e.target.value);
            setTested(false);
          }}
          placeholder=${keySet
            ? "Saved securely · leave blank to keep"
            : "Your Hermes API server key"}
          autocomplete="new-password"
      /></label>
      <p class="field-help">
        Your key is stored on the Talaria server and never sent back to your
        browser.
      </p>
      ${error && html`<div class="form-error" role="alert">${error}</div>`}
      ${tested &&
      html`<div class="form-success" role="status">
        <${Icon} name="check" size=${16} /> Hermes connection verified.
      </div>`}
      <div class="dialog-actions">
        <button
          type="button"
          class="button secondary"
          disabled=${busy}
          onClick=${() => submit(false)}
        >
          Test connection</button
        ><button class="button primary" disabled=${busy}>
          ${busy ? "Connecting…" : creating ? "Add profile" : "Save connection"}
        </button>
      </div>
    </form>
  `;
  return embedded
    ? content
    : html`<${Dialog} title=${initial ? "Meet your Hermes" : "Connection"} onClose=${onClose}>${content}</${Dialog}>`;
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
      if (mode === "delete") {
        writeStorage(`draft.${session.id}`, "");
        pendingStorage(`draft.${session.id}`, null).catch(() => {});
        pendingStorage(`run.${session.id}`, null).catch(() => {});
        forgetImages(session.id).catch(() => {});
        if (state.active === session.id) newConversation();
      }
      await refreshSessions();
      onClose();
      if (mode === "fork") {
        chooseReasoning(
          sessionReasoning({ ...state, active: session.id }),
          result.id || result.session_id || result.session?.id,
        );
        chooseModel(
          sessionModel({ ...state, active: session.id }),
          result.id || result.session_id || result.session?.id,
        );
        await openSession(result.id || result.session_id || result.session?.id);
      }
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

export function ModelPicker({
  models,
  selected,
  defaultModel,
  onSelect,
  onClose,
  onRefresh,
}) {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(80);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const filtered = models
    .filter((m) =>
      `${m.label} ${m.providerLabel} ${m.id}`
        .toLowerCase()
        .includes(query.toLowerCase()),
    )
    .sort(
      (a, b) =>
        Number(b.available !== false) - Number(a.available !== false) ||
        Number(!!b.featured) - Number(!!a.featured),
    );
  function select(model) {
    onSelect(model);
    onClose();
  }
  const warnings = [
    ...new Map(
      models
        .filter((m) => m.warning)
        .map((m) => [
          m.provider,
          { name: m.providerLabel, warning: m.warning },
        ]),
    ).values(),
  ];
  return html`<${Dialog} title="Choose a model" className="model-dialog" onClose=${onClose}>
    <p class="dialog-intro">Your model selection will only apply to this session.</p>
    <div class="model-search"><div class="search-field"><${Icon} name="search" size=${18}/><input aria-label="Search models" placeholder="Find a model…" value=${query} onInput=${(
      e,
    ) => {
      setQuery(e.target.value);
      setLimit(80);
    }} autoFocus/></div>
      <${IconButton} name="refresh" label="Refresh model catalog" disabled=${busy} onClick=${async () => {
        setBusy(true);
        setError("");
        try {
          await onRefresh();
        } catch (e) {
          setError(e.message);
        } finally {
          setBusy(false);
        }
      }} />
    </div>
    ${busy && html`<p class="field-help" role="status">Refreshing models from Hermes…</p>`}
    ${error && html`<div class="form-error" role="alert">${error}</div>`}
    ${
      warnings.length > 0 &&
      html`<details class="catalog-warnings">
        <summary>
          <${Icon} name="alert" size=${14} />Provider information ·
          ${warnings.length}
        </summary>
        ${warnings.map(
          (p) => html`<p><strong>${p.name}</strong> · ${p.warning}</p>`,
        )}
      </details>`
    }
    <div class="model-list"><button class="model-option model-default" disabled=${defaultModel?.available === false} onClick=${() => select(null)} aria-pressed=${!selected}><span>${defaultModel?.id || "Configured model"}<small>${defaultModel?.providerLabel || "Hermes"}</small></span><span class="model-option-end"><span class="default-badge">Default</span>${defaultModel?.available === false ? html`<span class="quiet-badge">Unavailable</span>` : !selected && html`<${Icon} name="check" size=${18} />`}</span></button>
    ${filtered
      .slice(0, limit)
      .map(
        (m) =>
          html`<button
            class="model-option"
            key=${`${m.provider}/${m.id}`}
            disabled=${m.available === false}
            aria-pressed=${selected?.id === m.id &&
            selected?.provider === m.provider}
            onClick=${() => select(m)}
          >
            <span
              >${m.label}<small>${m.providerLabel}</small>${m.pricing &&
              (m.pricing.input || m.pricing.output || m.pricing.free) &&
              html`<small class="model-price"
                >${m.pricing.free
                  ? "Free"
                  : `Per 1M tokens · Input ${m.pricing.input || "—"} · Output ${m.pricing.output || "—"}`}</small
              >`}</span
            ><span class="model-option-end"
              >${m.available === false
                ? html`<span class="quiet-badge">Unavailable</span>`
                : m.featured &&
                  html`<span class="quiet-badge"
                    >Featured</span
                  >`}${selected?.id === m.id &&
              selected?.provider === m.provider &&
              html`<${Icon} name="check" size=${18} />`}</span
            >
          </button>`,
      )}
    ${!filtered.length && html`<p class="field-help">${query ? "No models match that search." : "Your Hermes default is available. Additional models appear when Hermes provides them."}</p>`}
    ${filtered.length > limit && html`<button class="load-more" onClick=${() => setLimit(limit + 80)}>Show more models (${filtered.length - limit} remaining)</button>`}
    </div>
  </${Dialog}>`;
}

export function ReasoningPicker({ model, selected, onSelect, onClose }) {
  const options = reasoningOptions(model);
  return html`<${Dialog} title="Choose reasoning" onClose=${onClose}>
    <p class="dialog-intro">Your reasoning selection will only apply to this session.</p>
    ${model?.capabilities?.reasoning === false && html`<p class="field-help">This model does not support adjustable reasoning.</p>`}
    <div class="model-list reasoning-list">${options.map(
      (value) =>
        html`<button
          key=${value}
          class=${`model-option ${value === "auto" ? "model-default" : ""}`}
          aria-pressed=${selected === value}
          onClick=${() => {
            onSelect(value);
            onClose();
          }}
        >
          <span
            >${reasoningNames[value]}${value === "auto" &&
            html`<small
              >Use Hermes’s configured reasoning setting.</small
            >`}</span
          >
          <span class="model-option-end"
            >${value === "auto" &&
            html`<span class="default-badge">Default</span>`}${selected ===
              value && html`<${Icon} name="check" size=${18} />`}</span
          >
        </button>`,
    )}</div>
  </${Dialog}>`;
}
