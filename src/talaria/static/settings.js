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
import { Dialog } from "./dialogs.js";
import { ProfileConnections } from "./profiles.js";
import { refreshAgentInfo, refreshModels, supports, fail } from "./store.js";
import { ReadinessNotice } from "./readiness.js";

const sections = [
  ["agent", "spark", "Your agent"],
  ["appearance", "sun", "Appearance"],
  ["connection", "link", "Connection"],
  ["installation", "monitor", "Talaria"],
];
const readable = (value = "") =>
  value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function Facts({ items }) {
  return html`<dl class="settings-facts">
    ${items.map(
      ([label, value]) =>
        html`<div>
          <dt>${label}</dt>
          <dd>${value ?? "Not available"}</dd>
        </div>`,
    )}
  </dl>`;
}

function Installation() {
  const [info, setInfo] = useState(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    let active = true;
    api("/installation")
      .then((data) => {
        if (active) setInfo(data);
      })
      .catch(() => {
        if (active)
          setError(
            "Installation information could not be loaded. Reopen this section to try again.",
          );
      });
    return () => {
      active = false;
    };
  }, []);
  const date = (value) =>
    value && Number.isFinite(Date.parse(value))
      ? new Date(value).toLocaleString([], {
          dateStyle: "medium",
          timeStyle: "short",
        })
      : "Not available";
  if (!info)
    return html`<h3>Talaria</h3>
      <p class="field-help" role="status">
        ${error || "Loading installation information…"}
      </p>`;
  const development = info.environment === "development";
  return html`<div class="section-heading">
      <h3>Talaria</h3>
      <span class="${development ? "environment-badge" : "quiet-badge"}"
        >${development ? "Development" : "Production"}</span
      >
    </div>
    <${Facts}
      items=${[
        ["Version", info.version],
        [
          "Build",
          development
            ? "Working checkout"
            : info.commit?.slice(0, 10) || "Installed package",
        ],
        ...(info.managed
          ? [
              ["Update branch", info.branch],
              ["Installed", date(info.installed_at)],
            ]
          : []),
      ]}
    />
    ${!development && info.managed
        ? html`<section class="settings-section installation-update">
            <div class="section-heading">
              <h4>Updates</h4>
              ${!info.update.error &&
              info.update.checked_at &&
              html`<span
                class="${info.update.available
                  ? "default-badge"
                  : "quiet-badge"}"
                >${info.update.available
                  ? "Update available"
                  : "Up to date at last check"}</span
              >`}
            </div>
            <div class="update-command">
              <code>${info.update_command}</code
              ><button
                class="icon-button"
                aria-label="Copy update command"
                title="Copy update command"
                onClick=${async () => {
                  try {
                    await navigator.clipboard.writeText(info.update_command);
                    setCopied(true);
                    setError("");
                  } catch {
                    setError(
                      "Could not copy. Select the command to copy it manually.",
                    );
                  }
                }}
              >
                <${Icon} name=${copied ? "check" : "copy"} size=${16} />
              </button>
            </div>
            <span class="sr-only" role="status"
              >${copied ? "Update command copied" : ""}</span
            >
            <p class="field-help">
              ${info.update.checked_at
                ? `Last checked ${date(info.update.checked_at)}.`
                : "Updates have not been checked yet."}
            </p>
            ${info.update.error &&
            html`<p class="form-error" role="status">${info.update.error}</p>`}
            ${error && html`<p class="form-error" role="status">${error}</p>`}
          </section>`
        : !development && html`<p class="field-help">
            Update using your package manager.
          </p>`}`;
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
        >${app.connected ? "Connected" : "Offline"}</span
      >
    </div>
    <${ReadinessNotice} app=${app} compact />
    <section class="default-model-card">
      <div class="section-heading">
        <h4>Default model</h4>
        <span class="default-badge">Default</span>
      </div>
      <p>${model?.id || "Not shared by Hermes"}</p>
      <small
        >${model?.providerLabel || "Model information is not available."}</small
      >
    </section>
    <${Facts}
      items=${[
        ["Hermes version", info?.version || "Not available"],
        [
          "Gateway",
          info?.gateway_state ? readable(info.gateway_state) : "Not available",
        ],
        [
          "Health",
          info?.status === "ok"
            ? "Healthy"
            : info?.status
              ? readable(info.status)
              : "Not available",
        ],
      ]}
    />
    ${info?.platforms.length > 0 &&
    html`<section class="settings-section">
      <h4>Channels</h4>
      <div class="channel-list">
        ${info.platforms.map(
          (p) =>
            html`<span
              ><i
                class=${`connection-dot ${p.state === "connected" ? "connected" : ""}`}
              />${p.name === "api_server" ? "API" : readable(p.name)}<small
                >${readable(p.state)}</small
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
  return html`<section class="setting-section">
      <h3>Appearance</h3>
      <div class="appearance-options" role="group" aria-label="Appearance">
        ${[
          ["system", "monitor", "System"],
          ["light", "sun", "Light"],
          ["dark", "moon", "Dark"],
        ].map(
          ([id, icon, label]) =>
            html`<button
              class=${`appearance-option ${theme === id ? "selected" : ""}`}
              aria-pressed=${theme === id}
              onClick=${() => choose("theme", id)}
            >
              <${Icon} name=${icon} />${label}
            </button>`,
        )}
      </div>
    </section>
    <section class="setting-section">
      <h3>Accent color</h3>
      <div class="palette-options" role="group" aria-label="Accent color">
        ${["blue", "sage", "violet", "rose"].map(
          (id) =>
            html`<button
              class=${`palette-option ${palette === id ? "selected" : ""}`}
              aria-pressed=${palette === id}
              onClick=${() => choose("palette", id)}
            >
              <span class=${`swatch ${id}`}
                >${palette === id &&
                html`<${Icon} name="check" size=${16} />`}</span
              >${readable(id)}
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
  const [error, setError] = useState("");
  async function refresh() {
    setBusy(true);
    setError("");
    const results = await Promise.allSettled([
      refreshAgentInfo(),
      ...(supports("model_options") ? [refreshModels()] : []),
    ]);
    if (results.some((r) => r.status === "rejected"))
      setError(
        "Some agent information could not be refreshed. Your other settings are still available.",
      );
    setBusy(false);
  }
  useEffect(() => {
    refresh();
  }, []);
  return html`<${Dialog} title="Settings" onClose=${onClose} className="settings-dialog">
    <div class="settings-layout"><nav class="settings-navigation" role="tablist" aria-label="Settings sections">${sections.map(
      ([id, icon, label], index) =>
        html`<button
          id=${`settings-tab-${id}`}
          role="tab"
          aria-controls="settings-content"
          aria-selected=${section === id}
          tabindex=${section === id ? 0 : -1}
          onClick=${() => setSection(id)}
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
          <${Icon} name=${icon} size=${17} /><span>${label}</span>
        </button>`,
    )}</nav>
    <div key=${section} class="settings-panel" id="settings-content" role="tabpanel" aria-labelledby=${`settings-tab-${section}`} tabindex="0">
      ${error && html`<p class="form-error" role="alert">${error}</p>`}
      ${section === "agent" && html`<${Overview} app=${app} />`}
      ${section === "appearance" && html`<${Appearance} />`}
      ${section === "installation" && html`<${Installation} key=${installationRefresh} />`}
      ${
        section === "connection" &&
        html`<${ProfileConnections} app=${app} onRefresh=${refresh} />`
      }
    </div></div>
    <footer class="settings-footer"><span>Talaria ${app.version}</span><div><button disabled=${busy} onClick=${() => {
      if (section === "installation")
        setInstallationRefresh((value) => value + 1);
      else refresh();
    }}>${busy ? "Refreshing…" : "Refresh information"}</button><button onClick=${async () => {
      try {
        await api("/logout", { method: "POST", body: {} });
        location.reload();
      } catch (e) {
        fail(e);
      }
    }}>Sign out</button></div></footer>
  </${Dialog}>`;
}
