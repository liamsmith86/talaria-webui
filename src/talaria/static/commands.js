import { html, useEffect, useRef, useState, IconButton, readStorage, writeStorage } from "./lib.js";
import { api } from "./api.js";
import { state, update, newConversation, openSession, refreshSessions, navigationVersion } from "./store.js";

function savedCommand() {
  try {
    const value = JSON.parse(readStorage("command.pending", "null"));
    return value && typeof value.id === "string" && typeof value.session === "string" ? value : null;
  } catch {
    return null;
  }
}

function webCommand(name, args, app, controls) {
  if (args) throw new Error(`Use /${name} without arguments to open its controls.`);
  if (name === "new") return newConversation();
  if (name === "model") return controls.onModel();
  if (name === "reasoning") return controls.onReasoning();
  if (!app.active) throw new Error("Open a session first.");
  const types = { title: "rename", branch: "fork", status: "details", save: "download" };
  update({ modal: { type: types[name], session: app.sessionDetails || { id: app.active } } });
}

export function useCommands(app, draft, choose, controls) {
  const [catalog, setCatalog] = useState([]);
  const [selected, setSelected] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [pending, setPending] = useState(savedCommand);
  const [feedback, setFeedback] = useState(null);
  const [waiting, setWaiting] = useState(false);
  const enabled = app.caps.talaria_extensions?.commands === true;
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const query = /^\/([\w-]*)$/.exec(draft)?.[1]?.toLowerCase();
  const matches = query === undefined || dismissed ? [] : catalog.filter((c) =>
    [c.name, ...c.aliases].some((name) => name.startsWith(query)));
  const index = Math.min(selected, Math.max(0, matches.length - 1));
  const busy = !!pending && pending.session === (app.active || "");
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
  useEffect(() => {
    if (!pending) return;
    let current = true;
    let timer;
    const generation = navigationVersion();
    async function poll() {
      try {
        const result = await api(`/commands/${encodeURIComponent(pending.id)}`);
        if (!current) return;
        setWaiting(false);
        if (result.status === "running") {
          timer = setTimeout(poll, 1000);
          return;
        }
        setFeedback({ session: result.session_id || pending.session, text: result.text || result.error });
        setPending(null);
        writeStorage("command.pending", "null");
        if (result.status === "completed" && result.changed === true && result.session_id) {
          refreshSessions().catch(() => {});
          if (state.active === pending.session && navigationVersion() === generation)
            await openSession(pending.session);
        }
      } catch (error) {
        if (!current) return;
        if (error.status >= 400 && error.status < 500) {
          setFeedback({ session: pending.session, text: "Command result unavailable. Refresh the session before retrying." });
          setPending(null);
          writeStorage("command.pending", "null");
        } else {
          setWaiting(true);
          timer = setTimeout(poll, 5000);
        }
      }
    }
    // Submission has already settled before pending is set: never race its admission with GET.
    poll();
    return () => { current = false; clearTimeout(timer); };
  }, [pending]);

  async function submit(text, attachments) {
    const match = /^\/([a-z][\w-]*)(?:\s+([\s\S]*))?$/i.exec(text);
    if (!match) return false;
    if (attachments.length) throw new Error("Send attachments separately from commands.");
    if (!enabled) throw new Error("Install or update the Talaria plugin to use slash commands.");
    const command = catalog.find((c) => [c.name, ...c.aliases].includes(match[1].toLowerCase()));
    if (!command) throw new Error("Unknown command. Type / to see Hermes commands.");
    if (command.mode === "unavailable") throw new Error(command.reason);
    const args = (match[2] || "").trim();
    if (controls.active && !["help", "commands", "version", "profile", "egress", "bundles", "status"].includes(command.name))
      throw new Error("Wait for this response to finish before running the command.");
    if (["help", "commands"].includes(command.name)) {
      setFeedback({ session: app.active || "", text: catalog.map((c) =>
        `/${c.name} — ${c.mode === "unavailable" ? "Unavailable in Talaria" : c.description}`).join("\n") });
    } else if (command.mode === "web") {
      webCommand(command.name, args, app, controls);
    } else {
      if (pendingRef.current) throw new Error("Wait for the current command to finish.");
      if (command.name === "compress" && !app.active) throw new Error("Open a session first.");
      const job = { id: crypto.randomUUID(), session: app.active || "", command: command.name };
      // Persist before dispatch. An ambiguous response is recovered by GET, never a second POST.
      writeStorage("command.pending", JSON.stringify(job));
      const draftKey = `draft.${app.active || "new"}`;
      writeStorage(draftKey, "");
      try {
        await api("/commands", { method: "POST", body: {
          request_id: job.id, session_id: job.session, command: command.name, args,
        } });
      } catch (error) {
        if (error.status >= 400 && error.status < 500) {
          writeStorage("command.pending", "null");
          if (!readStorage(draftKey)) writeStorage(draftKey, text);
          throw error;
        }
      }
      pendingRef.current = job;
      setPending(job);
      setFeedback(null);
    }
    return true;
  }
  function keyDown(event) {
    if (!matches.length || event.isComposing) return false;
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      setSelected((index + (event.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length);
    } else if (event.key === "Escape") setDismissed(true);
    else if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) {
      choose(`/${matches[index].name} `);
    } else return false;
    event.preventDefault();
    return true;
  }
  const text = busy ? (waiting ? "Reconnecting to command…" : `/${pending.command}…`) :
    feedback?.session === (app.active || "") ? feedback.text : "";
  const panel = html`
    ${text && html`<div class="command-feedback" role="status"><pre>${text}</pre>
      ${!busy && html`<${IconButton} name="close" label="Dismiss command result" onClick=${() => setFeedback(null)} />`}
    </div>`}
    ${matches.length > 0 && html`<div class="command-picker" id="command-picker" role="listbox" aria-label="Hermes commands">
      ${matches.map((c, i) => html`<div key=${c.name} id=${`command-${i}`} role="option"
        aria-selected=${index === i} class=${index === i ? "selected" : ""}
        onMouseDown=${(event) => event.preventDefault()}
        onClick=${() => choose(`/${c.name} `)}>
        <strong>/${c.name}</strong>${c.args && html`<small>${c.args}</small>`}<span>${c.mode === "unavailable" ? c.reason : c.description}</span>
        ${c.mode === "unavailable" && html`<small class="command-unavailable">Unavailable</small>`}
      </div>`)}
    </div>`}`;
  return { submit, keyDown, panel, busy, expanded: !!matches.length, option: `command-${index}` };
}
