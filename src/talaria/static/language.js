import { html, Icon, useRef, useState } from "./lib.js";
import { languages, setLanguage, t, useLanguage } from "./i18n.js";

export function LanguageSelector() {
  const language = useLanguage();
  const [selection, setSelection] = useState(null);
  const [failed, setFailed] = useState(false);
  const request = useRef(0);
  return html`<div>
    <label class="language-control">
      <span><${Icon} name="globe" size=${16} />${t("Language")}</span>
      <select value=${selection ?? language.preference} aria-busy=${selection !== null}
        onChange=${async (event) => {
          const value = event.target.value;
          const generation = ++request.current;
          setSelection(value);
          setFailed(false);
          // Keep keyboard focus and permit changing one's mind during a fetch.
          // Both the global catalog and this local feedback use the latest choice.
          try { await setLanguage(value); }
          catch (_error) { if (generation === request.current) setFailed(true); }
          finally { if (generation === request.current) setSelection(null); }
        }}>
        <option value="auto">${t("System language")}</option>
        ${languages.map(({ code, name, dir }) => html`<option
          key=${code} value=${code} lang=${code} dir=${dir}>${name}</option>`)}
      </select>
    </label>
    ${failed && html`<p class="form-error" role="alert">${t("Could not load this language. Please try again.")}</p>`}
  </div>`;
}
