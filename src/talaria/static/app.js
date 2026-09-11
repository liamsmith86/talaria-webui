import {
  html,
  render,
  useEffect,
  useState,
  useRef,
  useMediaQuery,
  Icon,
  IconButton,
  Mark,
} from "./lib.js";
import { api } from "./api.js";
import {
  useStore,
  initialize,
  update,
  fail,
  newConversation,
  supports,
  chooseModel,
  chooseReasoning,
  refreshModels,
  openSession,
  state,
} from "./store.js";
import { Sidebar } from "./sidebar.js";
import { Conversation } from "./conversation.js";
import { Composer } from "./composer.js";
import {
  Connection,
  SessionDialog,
  ModelPicker,
  ReasoningPicker,
} from "./dialogs.js";
import {
  ConversationMenu,
  ConversationDetails,
  TranscriptDownload,
} from "./conversation-actions.js";
import { ReadinessNotice } from "./readiness.js";
import { Settings } from "./settings.js";
import { ProfilePicker } from "./profiles.js";
import { ContextIndicator, ResponseDetails } from "./message-insights.js";
import { MessageAction } from "./message-actions.js";
import { sessionModel, sessionReasoning } from "./models.js";
import { restoreRuns, running } from "./runs.js";

function Login({ development }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/login", { method: "POST", body: { password } });
      await initialize();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<main class="login-page">
    <div class="login-brand">
      <${Mark} size=${42} /><span>Talaria</span>
      ${development && html`<small class="environment-badge">Dev</small>`}
    </div>
    <div class="login-card">
      <h1>Sign in</h1>
      <form onSubmit=${submit}>
        <label class="field"
          >Password<input
            type="password"
            value=${password}
            onInput=${(e) => setPassword(e.target.value)}
            autocomplete="current-password"
            required
            autofocus
            placeholder="Your Talaria password"
        /></label>
        ${error &&
        html`<div class="form-error" role="alert">${error}</div>`}<button
          class="button primary"
          disabled=${busy}
        >
          ${busy ? "Loading…" : "Sign in"}<${Icon}
            name="arrow"
            size=${17}
          />
        </button>
      </form>
    </div>
    <p class="login-footer">Hermes Agent web user interface</p>
  </main>`;
}

function Welcome({ onSuggestion }) {
  const suggestions = [
    ["spark", "Plan", "Help me think through "],
    ["terminal", "Code", "I’d like to build "],
    ["file", "Write", "Help me write "],
  ];
  return html`<div class="welcome">
    <div class="welcome-mark"><${Mark} size=${49} /></div>
    <h1>New session</h1>
    <div class="suggestions">
      ${suggestions.map(
        ([icon, label, text]) =>
          html`<button
            onClick=${() => onSuggestion({ text, time: Date.now() })}
          >
            <${Icon} name=${icon} size=${19} /><span>${label}</span
            ><span class="suggestion-arrow">↗</span>
          </button>`,
      )}
    </div>
  </div>`;
}

function App() {
  const app = useStore();
  const mobile = useMediaQuery("(max-width: 700px)");
  const model = sessionModel(app);
  const [modelOpen, setModelOpen] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [suggestion, setSuggestion] = useState(null);
  const restored = useRef(false);
  useEffect(() => {
    initialize();
    const keydown = (e) => {
      if (
        e.defaultPrevented ||
        e.isComposing ||
        document.querySelector("dialog[open]")
      )
        return;
      if (
        (e.metaKey || e.ctrlKey) &&
        e.key.toLowerCase() === "f" &&
        state.active
      ) {
        e.preventDefault();
        update({ findOpen: true });
        requestAnimationFrame(() =>
          document
            .querySelector('[aria-label="Find text in session"]')
            ?.focus(),
        );
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        update({ sidebar: true });
        requestAnimationFrame(() =>
          document
            .querySelector('[aria-label="Search sessions"]')
            ?.focus(),
        );
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        newConversation();
      }
      if (e.key === "Escape") update({ sidebar: false });
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, []);
  useEffect(() => {
    if (app.connected && !restored.current) {
      restored.current = true;
      restoreRuns();
    }
  }, [app.connected]);
  const current = {
    id: app.active,
    title: "Session",
    ...app.sessionDetails,
    ...app.sessions.find((s) => s.id === app.active),
  };
  const close = () => update({ modal: null });
  if (app.auth === null)
    return html`<div class="initial-loader" role="status">
      <${Mark} size=${40} /><span>Loading…</span>
    </div>`;
  if (!app.auth)
    return html`<${Login} development=${app.environment === "development"} />`;
  return html`<div class="app-shell">
    <${Sidebar} app=${app} />${app.sidebar &&
    html`<button
      class="sidebar-overlay"
      tabindex="-1"
      aria-label="Close sidebar"
      onClick=${() => update({ sidebar: false })}
    />`}
    <main
      class=${`main-panel ${!app.active ? "is-new" : ""}`}
      inert=${mobile && app.sidebar}
    >
      <header class="topbar">
        <div class="topbar-left">
          <${IconButton}
            name="sidebar"
            label="Open sidebar"
            class="icon-button mobile-only"
            onClick=${() => update({ sidebar: true })}
          /><span class="topbar-title"
            >${app.active
              ? current.title || "Untitled session"
              : "New session"}</span
          >
          ${mobile &&
          app.environment === "development" &&
          html`<small class="environment-badge">Dev</small>`}
        </div>
        ${app.active &&
        html`<div class="topbar-actions">
          <${ContextIndicator} key=${app.active} app=${app} />
          <${IconButton}
            name="search"
            label="Find in session"
            aria-pressed=${app.findOpen}
            onClick=${() => update({ findOpen: !app.findOpen })}
          />
          <${IconButton}
            name="chart"
            label="Session details"
            onClick=${() =>
              update({ modal: { type: "details", session: current } })}
          />
          <${IconButton}
            name="more"
            label="Session options"
            onClick=${() =>
              update({ modal: { type: "session-menu", session: current } })}
          />
        </div>`}
      </header>
      <${ReadinessNotice} app=${app} />
      ${app.error &&
      html`<div class="error-banner" role="alert">
        <${Icon} name="alert" size=${17} /><span>${app.error}</span
        ><${IconButton}
          name="close"
          label="Dismiss error"
          onClick=${() => update({ error: "" })}
        />
      </div>`}
      ${!app.connected &&
      !app.connecting &&
      html`<div class="connection-banner">
        <span>Connect Hermes to start a session.</span
        ><button
          class="text-button"
          onClick=${() => update({ modal: "connection" })}
        >
          Set up connection<${Icon} name="link" size=${15} />
        </button>
      </div>`}
      ${app.connected &&
      (!supports("session_resources") || !supports("run_submission")) &&
      html`<div class="connection-banner" role="status">
        Update Hermes to a version that supports sessions and chat through
        its API.
      </div>`}
      ${app.active
        ? html`<${Conversation} key=${app.active} app=${app} />`
        : html`<${Welcome} onSuggestion=${setSuggestion} />`}
      ${app.readOnlyParent
        ? html`<div class="child-return">
            <span>Viewing a child session</span
            ><button
              class="text-button"
              onClick=${() => openSession(app.readOnlyParent.id)}
            >
              <${Icon} name="back" size=${17} />Back to parent session
            </button>
          </div>`
        : html`<div class="composer-area">
            <${Composer}
              app=${app}
              model=${model}
              onModel=${() => setModelOpen(true)}
              onReasoning=${() => setReasoningOpen(true)}
              draftSuggestion=${suggestion}
            />
            <p class="composer-hint">
              ${running(app.active)
                ? "Your agent keeps working if you leave."
                : "Shift + Enter for a new line"}
            </p>
          </div>`}
    </main>
    ${app.toast &&
    html`<div class="toast" role="status">
      <${Icon} name="check" size=${17} />${app.toast}
    </div>`}
    ${(app.modal === "settings" || app.modal?.type === "settings") &&
    html`<${Settings}
      app=${app}
      onClose=${close}
      initialSection=${app.modal?.section}
    />`}
    ${app.modal === "profiles" &&
    html`<${ProfilePicker} app=${app} onClose=${close} />`}
    ${app.modal === "connection" &&
    html`<${Connection} initial=${!app.connected} onClose=${close} />`}
    ${app.modal?.type === "session-menu" &&
    html`<${ConversationMenu}
      session=${app.modal.session}
      readOnly=${!!app.readOnlyParent && app.modal.session.id === app.active}
      onClose=${close}
    />`}
    ${app.modal?.type === "details" &&
    html`<${ConversationDetails}
      key=${app.modal.session.id}
      session=${app.modal.session}
      onClose=${close}
    />`}
    ${app.modal?.type === "response" &&
    html`<${ResponseDetails}
      session=${app.modal.session}
      message=${app.modal.message}
      enabled=${!!app.caps.talaria_extensions?.response_details}
      onClose=${close}
    />`}
    ${app.modal?.type === "message-action" &&
    html`<${MessageAction} ...${app.modal} onClose=${close} />`}
    ${app.modal?.type === "download" &&
    html`<${TranscriptDownload}
      session=${app.modal.session}
      onClose=${close}
    />`}
    ${["rename", "delete", "fork"].includes(app.modal?.type) &&
    html`<${SessionDialog}
      mode=${app.modal.type}
      session=${app.modal.session}
      onClose=${close}
    />`}
    ${modelOpen &&
    html`<${ModelPicker}
      models=${app.models}
      selected=${model}
      defaultModel=${app.defaultModel}
      onSelect=${(m) => chooseModel(m)}
      onRefresh=${() => refreshModels(true)}
      onClose=${() => setModelOpen(false)}
    />`}
    ${reasoningOpen &&
    html`<${ReasoningPicker}
      model=${model || app.defaultModel}
      selected=${sessionReasoning(app)}
      onSelect=${chooseReasoning}
      onClose=${() => setReasoningOpen(false)}
    />`}
  </div>`;
}
render(html`<${App} />`, document.getElementById("app"));
