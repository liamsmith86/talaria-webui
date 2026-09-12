import {
  html,
  useEffect,
  useRef,
  useState,
  useMemo,
  Icon,
  IconButton,
  writeStorage,
} from "./lib.js";
import { api } from "./api.js";
import { t, n, rich, formatNumber, msg } from "./i18n.js";
import { pendingStorage, forgetImages } from "./attachments.js";
import {
  state,
  connect,
  toast,
  refreshSessions,
  newConversation,
  openSession,
  chooseModel,
  chooseReasoning,
} from "./store.js";
import {
  sessionModel,
  sessionReasoning,
  reasoningLabel,
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
        label=${t("Close dialog")}
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
  const [keyFromEnv, setKeyFromEnv] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tested, setTested] = useState(false);
  useEffect(() => {
    if (creating) {
      setUrl((current) => state.profile?.server_url || state.profiles[0]?.server_url || current);
      return;
    }
    api("/connection")
      .then((d) => {
        setUrl(d.server_url || d.url);
        setProfile(d.profile || "default");
        setKeySet(d.key_set);
        setKeyFromEnv(d.key_from_env === true);
      })
      .catch((e) => setError(e.message));
  }, [creating]);
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
        toast(creating ? t("Profile added") : t("Connected to {name}", { name: state.agent.name }));
      } else setTested(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const content = html`
    <form
      onSubmit=${(e) => {
        e.preventDefault();
        submit(true);
      }}
    >
      ${creating &&
      html`<label class="field"
        >${t("Display name")}<input
          value=${label}
          onInput=${(e) => setLabel(e.target.value)}
          maxlength="80"
          required
          placeholder=${t("Research")}
      /></label>`}
      <label class="field"
        >${t("Hermes address")}<input
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
        >${t("Hermes profile")}<input
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
        ${rich("Use {profile} for the main agent, or the name of an existing Hermes profile. Named profiles need their own API key.", { profile: html`<code>default</code>` })}
      </p>
      <label class="field"
        >${t("API key")}<input
          type="password"
          value=${key}
          disabled=${keyFromEnv}
          onInput=${(e) => {
            setKey(e.target.value);
            setTested(false);
          }}
          placeholder=${keyFromEnv ? t("Managed by server environment") : keySet
            ? t("Leave blank to keep saved key")
            : t("Your Hermes API server key")}
          autocomplete="new-password"
      /></label>
      <p class="field-help">
        ${keyFromEnv ? t("Using {variable} from the server environment.", { variable: "TALARIA_HERMES_API_KEY" })
          : t("Your key is saved to {path}.", { path: creating || state.profile?.id !== "default"
            ? "~/.config/talaria/profiles.json" : "~/.config/talaria/config.json" })}
      </p>
      ${error && html`<div class="form-error" role="alert">${t(error)}</div>`}
      ${tested &&
      html`<div class="form-success" role="status">
        <${Icon} name="check" size=${16} /> ${t("Hermes connection verified.")}
      </div>`}
      <div class="dialog-actions">
        <button
          type="button"
          class="button secondary"
          disabled=${busy}
          onClick=${() => submit(false)}
        >
          ${t("Test connection")}</button
        ><button class="button primary" disabled=${busy}>
          ${busy ? t("Connecting…") : creating ? t("Add profile") : t("Save connection")}
        </button>
      </div>
    </form>
  `;
  return embedded
    ? content
    : html`<${Dialog} title=${initial ? t("Connect Hermes") : t("Connection")} onClose=${onClose}>${content}</${Dialog}>`;
}

export function SessionDialog({ mode, session, onClose }) {
  const [title, setTitle] = useState(
    mode === "fork" ? "" : session.title || null,
  );
  const displayedTitle = title ?? t("Untitled session");
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
          mode === "delete" || (mode === "fork" && !displayedTitle.trim())
            ? {}
            : { title: displayedTitle },
      });
      if (mode === "delete") {
        writeStorage(`draft.${session.id}`, "");
        pendingStorage(`draft.${session.id}`, null).catch(() => {});
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
          ? t("Session deleted")
          : mode === "fork"
            ? t("Session branched")
            : t("Session renamed"),
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<${Dialog} title=${t({ rename: msg("Rename session"), delete: msg("Delete session?"), fork: msg("Branch session") }[mode])} onClose=${onClose} dismissible=${!busy}>
    <form onSubmit=${submit}>
      ${mode === "delete" ? html`<p class="dialog-intro">${t("“{title}” will be permanently deleted from Hermes. This cannot be undone.", { title: displayedTitle })}</p>` : html`<label class="field">${t("Session name")}<input value=${displayedTitle} onInput=${(e) => setTitle(e.target.value)} placeholder=${mode === "fork" ? t("Automatic name from Hermes") : ""} maxlength="150" required=${mode !== "fork"} autofocus /></label>`}
      ${error && html`<div class="form-error" role="alert">${t(error)}</div>`}
      <div class="dialog-actions"><button type="button" class="button secondary" disabled=${busy} onClick=${onClose}>${t("Cancel")}</button><button disabled=${busy} class=${`button ${mode === "delete" ? "danger" : "primary"}`}>${busy ? t("Working…") : t({ rename: msg("Save name"), delete: msg("Delete session"), fork: msg("Create branch") }[mode])}</button></div>
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
  const filtered = useMemo(() => {
    const search = query.toLowerCase();
    return models
      .filter((m) =>
        `${m.label} ${m.providerLabel} ${m.id}`.toLowerCase().includes(search),
      )
      .sort(
        (a, b) =>
          Number(b.available !== false) - Number(a.available !== false) ||
          Number(!!b.featured) - Number(!!a.featured),
      );
  }, [models, query]);
  function select(model) {
    onSelect(model);
    onClose();
  }
  const warnings = useMemo(
    () => [
      ...new Map(
        models
          .filter((m) => m.warning)
          .map((m) => [
            m.provider,
            { name: m.providerLabel, warning: m.warning },
          ]),
      ).values(),
    ],
    [models],
  );
  return html`<${Dialog} title=${t("Choose a model")} className="model-dialog" onClose=${onClose}>
    <div class="model-search"><div class="search-field"><${Icon} name="search" size=${18}/><input aria-label=${t("Search models")} placeholder=${t("Find a model…")} value=${query} onInput=${(
      e,
    ) => {
      setQuery(e.target.value);
      setLimit(80);
    }} autoFocus/></div>
      <${IconButton} name="refresh" label=${t("Refresh model catalog")} disabled=${busy} onClick=${async () => {
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
    ${busy && html`<p class="field-help" role="status">${t("Refreshing models from Hermes…")}</p>`}
    ${error && html`<div class="form-error" role="alert">${t(error)}</div>`}
    ${
      warnings.length > 0 &&
      html`<details class="catalog-warnings">
        <summary>
          <${Icon} name="alert" size=${14} />${t("Provider information · {count}", { count: formatNumber(warnings.length) })}
        </summary>
        ${warnings.map(
          (p) => html`<p><strong>${p.name}</strong> · ${p.warning}</p>`,
        )}
      </details>`
    }
    <div class="model-list"><button class="model-option model-default" disabled=${defaultModel?.available === false} onClick=${() => select(null)} aria-pressed=${!selected}><span>${defaultModel?.id || t("Configured model")}<small>${defaultModel?.providerLabel || "Hermes"}</small></span><span class="model-option-end"><span class="default-badge">${t("Default")}</span>${defaultModel?.available === false ? html`<span class="quiet-badge">${t("Unavailable")}</span>` : !selected && html`<${Icon} name="check" size=${18} />`}</span></button>
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
                  ? t("Free")
                  : t("Per 1M tokens · Input {input} · Output {output}", { input: m.pricing.input || "—", output: m.pricing.output || "—" })}</small
              >`}</span
            ><span class="model-option-end"
              >${m.available === false
                ? html`<span class="quiet-badge">${t("Unavailable")}</span>`
                : m.featured &&
                  html`<span class="quiet-badge"
                    >${t("Featured")}</span
                  >`}${selected?.id === m.id &&
              selected?.provider === m.provider &&
              html`<${Icon} name="check" size=${18} />`}</span
            >
          </button>`,
      )}
    ${!filtered.length && html`<p class="field-help">${query ? t("No models match that search.") : t("Your Hermes default is available. Additional models appear when Hermes provides them.")}</p>`}
    ${filtered.length > limit && html`<button class="load-more" onClick=${() => setLimit(limit + 80)}>${n("Show more models ({count} remaining)", "Show more models ({count} remaining)", filtered.length - limit)}</button>`}
    </div>
  </${Dialog}>`;
}

export function ReasoningPicker({ model, selected, onSelect, onClose }) {
  const options = reasoningOptions(model);
  return html`<${Dialog} title=${t("Choose reasoning")} onClose=${onClose}>
    ${model?.capabilities?.reasoning === false && html`<p class="field-help">${t("This model does not support adjustable reasoning.")}</p>`}
    ${model?.capabilities?.reasoning !== false && !Array.isArray(model?.capabilities?.supported_efforts) && html`<p class="field-help">${t("Hermes may adjust the level to match the model’s supported settings.")}</p>`}
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
            >${reasoningLabel(value, model)}${value === "auto" &&
            html`<small
              >${t("Use Hermes’s configured reasoning setting.")}</small
            >`}</span
          >
          <span class="model-option-end"
            >${value === "auto" &&
            html`<span class="default-badge">${t("Default")}</span>`}${selected ===
              value && html`<${Icon} name="check" size=${18} />`}</span
          >
        </button>`,
    )}</div>
  </${Dialog}>`;
}
