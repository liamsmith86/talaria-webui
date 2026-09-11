import { html, useRef, useState, Icon } from "./lib.js";
import { answerClarification } from "./runs.js";

export function Clarification({ sid, request }) {
  const [selected, setSelected] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const choices = Array.isArray(request.choices)
    ? request.choices.filter((choice) => typeof choice === "string").slice(0, 4) : [];
  const multi = request.multi_select === true && choices.length > 0;
  async function respond(value) {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      await answerClarification(sid, request.request_id, value);
    } catch (e) {
      setError(e.message);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  function submit(event) {
    event.preventDefault();
    const values = [...selected, ...(text.trim() ? [text.trim()] : [])];
    if (multi ? values.length : text.trim())
      respond(multi ? JSON.stringify(values) : text.trim());
  }
  return html`<section class="clarification-card" aria-label="Question from your agent">
    <div class="clarification-heading"><${Icon} name="message" size=${18} />
      <span>Your input</span></div>
    <p class="clarification-question">${request.question}</p>
    <form onSubmit=${submit}>
      <div class="clarification-choices">
        ${choices.map((choice) => multi
          ? html`<label key=${choice} class="clarification-choice">
              <input type="checkbox" checked=${selected.includes(choice)} disabled=${busy}
                onChange=${(event) => setSelected(event.currentTarget.checked
                  ? [...selected, choice] : selected.filter((value) => value !== choice))} />
              <span>${choice}</span>
            </label>`
          : html`<button key=${choice} type="button" class="clarification-choice"
              disabled=${busy} onClick=${() => respond(choice)}>${choice}</button>`)}
      </div>
      <label class="clarification-answer">
        <span>${choices.length ? "Or write an answer" : "Your answer"}</span>
        <textarea rows="2" maxlength="16000" value=${text} disabled=${busy}
          onInput=${(event) => setText(event.currentTarget.value)} />
      </label>
      ${error && html`<div class="form-error" role="alert">${error}</div>`}
      <div class="clarification-actions">
        <button type="button" class="button secondary" disabled=${busy}
          onClick=${() => respond("")}>Skip</button>
        <button type="submit" class="button primary"
          disabled=${busy || (!text.trim() && !(multi && selected.length))}>
          ${busy ? "Sending…" : "Send answer"}</button>
      </div>
    </form>
  </section>`;
}
