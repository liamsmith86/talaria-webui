import { html, useEffect, useState, Icon } from "./lib.js";
import { activeProfile } from "./profile-context.js";
import { siteURL } from "./paths.js";
import { api } from "./api.js";
import { Dialog, Connection } from "./dialogs.js";
import { ExtendedAccess } from "./access-settings.js";
import { update, refreshProfiles, toast } from "./store.js";
import { msg, t } from "./i18n.js";

const profileHref = (id) => siteURL(`?profile=${encodeURIComponent(id)}`);

export function ProfileSwitch({ app }) {
  return html`<button
    class="agent-switcher"
    aria-label=${t("Switch profile")}
    aria-haspopup="dialog"
    onClick=${() => update({ modal: "profiles" })}
  >
    <${Icon} name="branch" size=${17} /><span
      ><small>${t("Profile")}</small
      ><strong>${app.profile?.label || app.agent.name}</strong></span
    ><${Icon} name="chevron" size=${15} />
  </button>`;
}

export function ProfilePicker({ app, onClose }) {
  const [error, setError] = useState("");
  useEffect(() => {
    refreshProfiles()
      .then((d) => setError(d.error || ""))
      .catch((e) => setError(e.message));
  }, []);
  return html`<${Dialog} title=${t("Choose a profile")} onClose=${onClose} dismissible=${!app.profileMissing}>
    ${app.profileMissing && html`<p class="dialog-intro">${t("This profile is no longer available. Choose another.")}</p>`}
    ${error && html`<p class="form-error" role="status">${t(error)}</p>`}
    <div class="profile-options">${app.profiles.map(
      (profile) =>
        html`<a
          class="profile-option"
          href=${profileHref(profile.id)}
          aria-current=${profile.id === activeProfile ? "page" : undefined}
          onClick=${(event) => {
            if (profile.id === activeProfile) {
              event.preventDefault();
              onClose();
            }
          }}
        >
          <span class="profile-monogram" aria-hidden="true"
            >${profile.label.slice(0, 1)}</span
          ><span
            ><strong>${profile.label}</strong
            ><small
              >${profile.profile} · ${new URL(profile.server_url).host}</small
            ></span
          >
          ${profile.id === activeProfile &&
          html`<${Icon} name="check" size=${18} />`}
        </a>`,
    )}</div>
    <div class="dialog-actions"><button class="button secondary" onClick=${() => update({ modal: { type: "settings", section: "connection" } })}>${t("Manage profiles")}</button></div>
  </${Dialog}>`;
}

export function ProfileConnections({ app, onRefresh, readOnly = false }) {
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    refreshProfiles()
      .then((d) => setError(d.error || ""))
      .catch((e) => setError(e.message));
  }, []);
  async function remove(profile) {
    setBusy(true);
    setError("");
    try {
      await api(`/profiles/${profile.id}`, { method: "DELETE", body: {} });
      await refreshProfiles();
      setRemoving(null);
      toast(msg("Profile removed from Talaria"));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  if (adding)
    return html`<div class="section-heading">
        <h3>${t("Add profile")}</h3>
        <button class="text-button" onClick=${() => setAdding(false)}>
          ${t("Back")}
        </button>
      </div>
      <${Connection}
        key="new-profile"
        embedded
        creating
        onClose=${async () => {
          await refreshProfiles();
          setAdding(false);
        }}
      />`;
  return html`<section class="settings-section">
      <div class="section-heading">
        <h3>${t("Profiles")}</h3>
        <button
          class="text-button"
          disabled=${readOnly || app.profiles.length >= 8}
          onClick=${() => setAdding(true)}
        >
          <${Icon} name="plus" size=${14} /> ${t("Add profile")}
        </button>
      </div>
      <div class="saved-profiles">
        ${app.profiles.map(
          (profile) =>
            html`<div class="saved-profile">
              <span
                ><strong>${profile.label}</strong
                ><small>${profile.profile}</small></span
              >
              ${profile.id === activeProfile
                ? html`<span class="quiet-badge">${t("Current")}</span>`
                : html`<a class="text-button" href=${profileHref(profile.id)}
                    >${t("Switch")}</a
                  >`}
              ${profile.id !== "default" &&
              profile.id !== activeProfile &&
              html`<button
                class="icon-button"
                disabled=${readOnly}
                aria-label=${t("Remove {name}", { name: profile.label })}
                title=${t("Remove profile")}
                onClick=${() => setRemoving(profile)}
              >
                <${Icon} name="close" size=${15} />
              </button>`}
            </div>`,
        )}
      </div>
      ${removing &&
      html`<div class="profile-removal">
        <p>
          ${t("Remove {name} from Talaria? Its sessions stay in Hermes.", { name: removing.label })}
        </p>
        <div class="dialog-actions">
          <button
            class="button secondary"
            disabled=${busy}
            onClick=${() => setRemoving(null)}
          >
            ${t("Cancel")}</button
          ><button
            class="button danger"
            disabled=${busy}
            onClick=${() => remove(removing)}
          >
            ${busy ? t("Removing…") : t("Remove profile")}
          </button>
        </div>
      </div>`}
      ${error && html`<p class="form-error" role="alert">${t(error)}</p>`}
    </section>
    ${!app.profileMissing &&
    html`<section class="settings-section profile-connection">
        <h3>${app.profile?.label || t("Hermes API")}</h3>
        <${Connection} embedded readOnly=${readOnly} onClose=${onRefresh} />
      </section>
      <${ExtendedAccess} readOnly=${readOnly} onSaved=${onRefresh} />`}`;
}
