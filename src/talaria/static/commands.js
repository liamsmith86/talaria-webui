import { html, randomId, useEffect, useRef, useState } from "./lib.js";
import { api } from "./api.js";
import { state, supports, update, newConversation } from "./store.js";
import { sendMessage, stopRun } from "./runs.js";
import { msg, t } from "./i18n.js";

// Hermes offers these to its own clients as commands that Talaria shows as
// controls. Its text for /stop describes background processes, not a response.
const composerCommands = {
  stop: { feature: "run_stop", description: msg("Stop the current response") },
  steer: { feature: "run_steer", args: "<prompt>" },
};
export const commandDescription = (command) =>
  command.localDescription ? t(command.localDescription) : command.description;
const listed = (command) => command.mode !== "unavailable";

function offered(command) {
  const { feature, description, args = "" } = composerCommands[command.name] || {};
  return feature && supports(feature)
    ? { ...command, mode: "web", reason: "", args, localDescription: description }
    : command;
}

async function steer(args, app, controls) {
  if (!args) throw new Error(msg("Type your guidance after /steer."));
  if (!controls.active) throw new Error(msg("No response is running."));
  if (state.lives[app.active]?.clarification)
    throw new Error(msg("Answer the question before sending guidance."));
  await sendMessage(args);
}

async function stop(args, app, controls) {
  if (args) throw new Error(t("Use /{command} without arguments.", { command: "stop" }));
  if (!controls.active) throw new Error(msg("No response is running."));
  if (!state.lives[app.active]?.id)
    throw new Error(msg("Wait for the current message to finish sending."));
  await stopRun(app.active);
}

function webCommand(name, args, app, controls) {
  if (name === "steer") return steer(args, app, controls);
  if (name === "stop") return stop(args, app, controls);
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
    listed(c) && [c.name, ...c.aliases].some((name) => name.startsWith(query)));
  const index = Math.min(selected, Math.max(0, matches.length - 1));
  const busy = activity.busy;
  const loading = useRef(null);
  // One request serves the picker and a command sent before the picker loaded.
  // A failed load is tried again when needed, never taken as an empty catalog.
  function loadCatalog() {
    loading.current ??= api("/commands").then((data) => (data.commands || []).map(offered))
      .catch((error) => {
        loading.current = null;
        throw error;
      });
    return loading.current;
  }
  useEffect(() => {
    setSelected(0);
    setDismissed(false);
  }, [draft, app.active]);
  // Another plugin release may offer other commands.
  const release = app.caps.talaria_extensions?.release?.version;
  useEffect(() => {
    setCatalog([]);
    loading.current = null;
  }, [enabled, release]);
  // Opening the picker, on "/", asks again for a catalog that failed to load.
  const picking = query !== undefined;
  useEffect(() => {
    if (!enabled || catalog.length || loading.current) return;
    const request = loadCatalog();
    request.then((list) => {
      if (loading.current === request) setCatalog(list);
    }).catch(() => {});
  }, [enabled, release, picking, catalog.length]);
  async function submit(text, attachments) {
    const match = /^\/([a-z][\w-]*)(?:\s+([\s\S]*))?$/i.exec(text);
    if (!match) return false;
    if (attachments.length) throw new Error(msg("Send attachments separately from commands."));
    if (!enabled) throw new Error(msg("Install or update the Talaria plugin to use slash commands."));
    const commands = catalog.length ? catalog : await loadCatalog();
    const command = commands.find((c) => [c.name, ...c.aliases].includes(match[1].toLowerCase()));
    if (!command) throw new Error(msg("Unknown command. Type / to see Hermes commands."));
    if (command.mode === "unavailable") throw new Error(command.reason);
    const args = (match[2] || "").trim();
    if (controls.active && !["help", "commands", "version", "profile", "egress", "bundles", "status", "stop", "steer"].includes(command.name))
      throw new Error(msg("Wait for this response to finish before running the command."));
    if (["help", "commands"].includes(command.name)) {
      activity.show({ id: randomId(), command: command.name, status: "completed", session: app.active || "", commands: commands.filter(listed) });
    } else if (command.mode === "web") {
      await webCommand(command.name, args, app, controls);
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
        <strong>/${c.name}</strong>${c.args && html`<small>${c.args}</small>`}<span>${commandDescription(c)}</span>
      </div>`)}
    </div>`}`;
  return { submit, keyDown, panel, busy, expanded: !!matches.length, option: `command-${index}` };
}
