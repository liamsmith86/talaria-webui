import { html, useEffect, useState } from "./lib.js";
import { commandRunning } from "./command-activity.js";
import { api } from "./api.js";
import { update, newConversation } from "./store.js";
import { msg, t } from "./i18n.js";

function webCommand(name, args, app, controls) {
  if (args) throw new Error(t("Use /{command} without arguments to open its controls.", { command: name }));
  if (name === "new") return newConversation();
  if (name === "model") return controls.onModel();
  if (name === "reasoning") return controls.onReasoning();
  if (!app.active) throw new Error(msg("Open a session first."));
  const types = { title: "rename", branch: "fork", status: "details", save: "download" };
  update({ modal: { type: types[name], session: app.sessionDetails || { id: app.active } } });
}

export function useCommands(app, draft, choose, controls, activity) {
  const [catalog, setCatalog] = useState([]);
  const [selected, setSelected] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const enabled = app.caps.talaria_extensions?.commands === true;
  const query = /^\/([\w-]*)$/.exec(draft)?.[1]?.toLowerCase();
  const matches = query === undefined || dismissed ? [] : catalog.filter((c) =>
    [c.name, ...c.aliases].some((name) => name.startsWith(query)));
  const index = Math.min(selected, Math.max(0, matches.length - 1));
  const busy = commandRunning(activity.current) && activity.current.session === (app.active || "");
  useEffect(() => {
    setSelected(0);
    setDismissed(false);
  }, [draft, app.active]);
  useEffect(() => {
    let current = true;
    setCatalog([]);
    if (enabled) api("/commands").then((data) => {
      if (current) setCatalog((data.commands || []).toSorted((a, b) =>
        Number(a.mode === "unavailable") - Number(b.mode === "unavailable")));
    }).catch(() => {});
    return () => { current = false; };
  }, [enabled, app.caps.talaria_extensions?.release?.version]);
  async function submit(text, attachments) {
    const match = /^\/([a-z][\w-]*)(?:\s+([\s\S]*))?$/i.exec(text);
    if (!match) return false;
    if (attachments.length) throw new Error(msg("Send attachments separately from commands."));
    if (!enabled) throw new Error(msg("Install or update the Talaria plugin to use slash commands."));
    const command = catalog.find((c) => [c.name, ...c.aliases].includes(match[1].toLowerCase()));
    if (!command) throw new Error(msg("Unknown command. Type / to see Hermes commands."));
    if (command.mode === "unavailable") throw new Error(command.reason);
    const args = (match[2] || "").trim();
    if (controls.active && !["help", "commands", "version", "profile", "egress", "bundles", "status"].includes(command.name))
      throw new Error(msg("Wait for this response to finish before running the command."));
    if (["help", "commands"].includes(command.name)) {
      activity.show({ id: crypto.randomUUID(), command: command.name, status: "completed", afterId: app.history.at(-1)?.id || 0, session: app.active || "", commands: catalog });
    } else if (command.mode === "web") {
      webCommand(command.name, args, app, controls);
    } else {
      if (command.name === "compress" && !app.active) throw new Error(msg("Open a session first."));
      await activity.start(command.name, args, app.active || "", text);
    }
    return true;
  }
  function keyDown(event) {
    if (!matches.length) return false;
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      setSelected((index + (event.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length);
    } else if (event.key === "Escape") setDismissed(true);
    else if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) {
      choose(`/${matches[index].name} `);
    } else return false;
    event.preventDefault();
    return true;
  }
  const panel = html`
    ${matches.length > 0 && html`<div class="command-picker" id="command-picker" role="listbox" aria-label=${t("Hermes commands")}>
      ${matches.map((c, i) => html`<div key=${c.name} id=${`command-${i}`} role="option"
        aria-selected=${index === i} class=${index === i ? "selected" : ""}
        onMouseDown=${(event) => event.preventDefault()}
        onClick=${() => choose(`/${c.name} `)}>
        <strong>/${c.name}</strong>${c.args && html`<small>${c.args}</small>`}<span>${c.mode === "unavailable" ? c.reason : c.description}</span>
        ${c.mode === "unavailable" && html`<small class="command-unavailable">${t("Unavailable")}</small>`}
      </div>`)}
    </div>`}`;
  return { submit, keyDown, panel, busy, expanded: !!matches.length, option: `command-${index}` };
}
