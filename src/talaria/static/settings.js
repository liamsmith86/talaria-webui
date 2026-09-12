import {
  html,
  useEffect,
  useState,
  Icon,
  readStorage,
  writeStorage,
  useMediaQuery,
} from "./lib.js";
import { api } from "./api.js";
import { Installation } from "./installation.js";
import { Dialog } from "./dialogs.js";
import { ProfileConnections } from "./profiles.js";
import { refreshAgentInfo, refreshModels, supports, fail } from "./store.js";
import { ReadinessNotice } from "./readiness.js";
import { msg, t } from "./i18n.js";
import { LanguageSelector } from "./language.js";
import { statusLabel } from "./content.js";

const sections = [
  ["agent", "spark", msg("Your agent")],
  ["appearance", "sun", msg("Appearance")],
  ["connection", "link", msg("Connection")],
  ["installation", "monitor", "Talaria"],
];
const readable = (value = "") =>
  value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function Facts({ items }) {
  return html`<dl class="settings-facts">
    ${items.map(
      ([label, value]) =>
        html`<div>
          <dt>${t(label)}</dt>
          <dd>${value ?? t("Not available")}</dd>
        </div>`,
    )}
  </dl>`;
}

function Overview({ app }) {
  const info = app.agentInfo,
    model = app.defaultModel;
  return html`<div class="agent-overview">
    <div class="agent-identity">
      <span class="agent-monogram" aria-hidden="true"
        >${app.agent.name.slice(0, 1)}</span
      >
      <div>
        <h3>${app.agent.name}</h3>
        <p>Hermes Agent</p>
      </div>
      <span class=${`status-badge ${app.connected ? "positive" : ""}`}
        >${app.connected ? t("Connected") : t("Offline")}</span
      >
    </div>
    <${ReadinessNotice} app=${app} compact />
    <section class="default-model-card">
      <div class="section-heading">
        <h4>${t("Default model")}</h4>
        <span class="default-badge">${t("Default")}</span>
      </div>
      <p>${model?.id || t("Not shared by Hermes")}</p>
      <small
        >${model?.providerLabel || t("Model information is not available.")}</small
      >
    </section>
    <${Facts}
      items=${[
        [msg("Hermes version"), info?.version || t("Not available")],
        [
          msg("Gateway"),
          info?.gateway_state ? statusLabel(info.gateway_state) : t("Not available"),
        ],
        [
          msg("Health"),
          info?.status === "ok"
            ? t("Healthy")
            : info?.status
              ? statusLabel(info.status)
              : t("Not available"),
        ],
      ]}
    />
    ${info?.platforms.length > 0 &&
    html`<section class="settings-section">
      <h4>${t("Channels")}</h4>
      <div class="channel-list">
        ${info.platforms.map(
          (p) =>
            html`<span
              ><i
                class=${`connection-dot ${p.state === "connected" ? "connected" : ""}`}
              />${p.name === "api_server" ? "API" : readable(p.name)}<small
                >${statusLabel(p.state)}</small
              ></span
            >`,
        )}
      </div>
    </section>`}
  </div>`;
}

function Appearance() {
  const [theme, setTheme] = useState(readStorage("theme", "system"));
  const [palette, setPalette] = useState(readStorage("palette", "blue"));
  function choose(key, value) {
    writeStorage(key, value);
    document.documentElement.dataset[key] = value;
    (key === "theme" ? setTheme : setPalette)(value);
  }
  return html`<section class="setting-section"><${LanguageSelector} /></section>
    <section class="setting-section">
      <h3>${t("Appearance")}</h3>
      <div class="appearance-options" role="group" aria-label=${t("Appearance")}>
        ${[
          ["system", "monitor", msg("System")],
          ["light", "sun", msg("Light")],
          ["dark", "moon", msg("Dark")],
        ].map(
          ([id, icon, label]) =>
            html`<button
              class=${`appearance-option ${theme === id ? "selected" : ""}`}
              aria-pressed=${theme === id}
              onClick=${() => choose("theme", id)}
            >
              <${Icon} name=${icon} />${t(label)}
            </button>`,
        )}
      </div>
    </section>
    <section class="setting-section">
      <h3>${t("Accent color")}</h3>
      <div class="palette-options" role="group" aria-label=${t("Accent color")}>
        ${[["blue", msg("Blue")], ["sage", msg("Sage")], ["violet", msg("Violet")], ["rose", msg("Rose")]].map(
          ([id, label]) =>
            html`<button
              class=${`palette-option ${palette === id ? "selected" : ""}`}
              aria-pressed=${palette === id}
              onClick=${() => choose("palette", id)}
            >
              <span class=${`swatch ${id}`}
                >${palette === id &&
                html`<${Icon} name="check" size=${16} />`}</span
              >${t(label)}
            </button>`,
        )}
      </div>
    </section>`;
}

export function Settings({ app, onClose, initialSection = "agent" }) {
  const [section, setSection] = useState(initialSection);
  const [installationRefresh, setInstallationRefresh] = useState(0);
  const mobile = useMediaQuery("(max-width: 700px)");
  useEffect(() => {
    if (mobile)
      document
        .getElementById(`settings-tab-${section}`)
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [section, mobile]);
  const [busy, setBusy] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  async function refresh() {
    setBusy(true);
    setRefreshFailed(false);
    const results = await Promise.allSettled([
      refreshAgentInfo(),
      ...(supports("model_options") ? [refreshModels()] : []),
    ]);
    if (results.some((r) => r.status === "rejected"))
      setRefreshFailed(true);
    setBusy(false);
  }
  useEffect(() => {
    refresh();
  }, []);
  return html`<${Dialog} title=${t("Settings")} onClose=${onClose} className="settings-dialog">
    <div class="settings-layout"><nav class="settings-navigation" role="tablist" aria-label=${t("Settings sections")}>${sections.map(
      ([id, icon, label], index) =>
        html`<button
          id=${`settings-tab-${id}`}
          role="tab"
          aria-controls="settings-content"
          aria-selected=${section === id}
          tabindex=${section === id ? 0 : -1}
          onClick=${() => {
            setSection(id);
            if (id === "installation") setInstallationRefresh((value) => value + 1);
          }}
          onKeyDown=${(e) => {
            const next =
              e.key === "Home"
                ? 0
                : e.key === "End"
                  ? sections.length - 1
                  : ["ArrowRight", "ArrowDown"].includes(e.key)
                    ? (index + 1) % sections.length
                    : ["ArrowLeft", "ArrowUp"].includes(e.key)
                      ? (index + sections.length - 1) % sections.length
                      : null;
            if (next !== null) {
              e.preventDefault();
              setSection(sections[next][0]);
              document
                .getElementById(`settings-tab-${sections[next][0]}`)
                ?.focus();
            }
          }}
        >
          <${Icon} name=${icon} size=${17} /><span>${t(label)}</span>
        </button>`,
    )}</nav>
    <div key=${section} class="settings-panel" id="settings-content" role="tabpanel" aria-labelledby=${`settings-tab-${section}`} tabindex="0">
      ${refreshFailed && html`<p class="form-error" role="alert">${t("Some agent information could not be refreshed. Your other settings are still available.")}</p>`}
      ${section === "agent" && html`<${Overview} app=${app} />`}
      ${section === "appearance" && html`<${Appearance} />`}
      ${section === "installation" && html`<${Installation} key=${installationRefresh} />`}
      ${
        section === "connection" &&
        html`<${ProfileConnections} app=${app} onRefresh=${refresh} />`
      }
    </div></div>
    <footer class="settings-footer"><span>Talaria ${app.version}</span><div>${section !== "installation" && html`<button disabled=${busy} onClick=${refresh}>${busy ? t("Refreshing…") : t("Refresh information")}</button>`}<button onClick=${async () => {
      try {
        await api("/logout", { method: "POST", body: {} });
        location.reload();
      } catch (e) {
        fail(e);
      }
    }}>${t("Sign out")}</button></div></footer>
  </${Dialog}>`;
}
