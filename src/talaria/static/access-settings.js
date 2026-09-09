import { html, useEffect, useState, Icon } from "./lib.js";
import { api } from "./api.js";
import { update, toast } from "./store.js";

const messages = {
  disabled: "Extended access is off. Your Hermes API connection is unaffected.",
  not_found:
    "That directory is not available on this server. API values and defaults will still work.",
  unreadable:
    "Talaria could not read the identity files. Check the path and file permissions. API values and defaults will still work.",
  unsupported:
    "The identity files must be regular files. API values and defaults will still work.",
  no_name:
    "No supported agent name was found. API values and defaults will still work.",
};

export function ExtendedAccess({ onSaved }) {
  const [path, setPath] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    api("/hermes-access")
      .then((data) => {
        setPath(data.path);
        setResult(data);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);
  async function submit(save) {
    setBusy(true);
    setError("");
    try {
      const data = await api(save ? "/hermes-access" : "/hermes-access/test", {
        method: save ? "PUT" : "POST",
        body: { path },
      });
      setResult(data);
      if (save) {
        setPath(data.path);
        update({ agent: data.agent });
        toast("Extended access saved");
        onSaved();
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return html`<section class="extended-access">
    <div class="section-heading">
      <h3>Extended access</h3>
      <span class="quiet-badge">Optional · Read-only</span>
    </div>
    <p class="dialog-intro">
      Read agent details that aren’t available through the Hermes API.
    </p>
    <form
      onSubmit=${(e) => {
        e.preventDefault();
        submit(true);
      }}
    >
      <label class="field"
        >Hermes directory<input
          value=${path}
          onInput=${(e) => {
            setPath(e.target.value);
            setResult(null);
          }}
          placeholder="~/.hermes"
          maxlength="4096"
          spellcheck="false"
          disabled=${loading || busy}
      /></label>
      <p class="field-help">
        Files on this server. Choose the directory containing Hermes’s
        config.yaml and SOUL.md. Leave empty to turn off extended access.
      </p>
      <p class="access-scope">
        Currently reads: agent name from IDENTITY.md or SOUL.md. Your files are
        never changed.
      </p>
      ${result &&
      html`<div class="access-status" role="status">
        <${Icon}
          name=${result.status === "ready" ? "check" : "file"}
          size=${16}
        /><span
          >${result.status === "ready"
            ? `Found ${result.name} in ${result.source}.`
            : messages[result.status] || messages.unreadable}</span
        >
      </div>`}
      ${error && html`<p class="form-error" role="alert">${error}</p>`}
      <div class="dialog-actions">
        <button
          type="button"
          class="button secondary"
          disabled=${busy || loading}
          onClick=${() => submit(false)}
        >
          Test access</button
        ><button class="button primary" disabled=${busy || loading}>
          ${busy ? "Checking…" : "Save access"}
        </button>
      </div>
    </form>
  </section>`;
}
