// A tab-local transport for the normal chat UI. Visitor input never leaves the browser.
import { api, RequestError, setTransport } from "./api.js";
import { randomId } from "./lib.js";
import { plainContent } from "./content.js";
import { rememberSession, selectedSession } from "./session-navigation.js";
import { msg } from "./i18n.js";

const model = "claude-sonnet-4-6", provider = "anthropic";
const profile = { id: "default", label: "Hermes", profile: "default", server_url: "http://127.0.0.1:8642" };
const extensions = { version: 1, rewind: true, commands: true, context_usage: true, response_details: true, history_search: true,
  context_runs: true, release: { version: "1.4.0", status: "current" } };
const readiness = { status: "ok", issues: [] };
const summary = ({ messages, ...session }) => ({ ...session, message_count: messages.length });
let enabled = false;

export async function enableDemo(signal, version) {
  if (enabled) return;
  const samples = await api("/samples", { signal });
  signal.throwIfAborted();
  const sessions = new Map(), runs = new Map(), jobs = new Map();
  let sequence = 1000;
  const row = (session, data) => ({ ...data, id: ++sequence, session_id: session.id, timestamp: Date.now() / 1000 });
  const create = (data) => {
    const session = { created_at: Date.now() / 1000, updated_at: Date.now() / 1000,
      source: "api_server", model, provider, input_tokens: 9300, output_tokens: 840,
      cache_read_tokens: 4096, reasoning_tokens: 160, api_call_count: 2, ...data };
    session.messages = (data.messages || []).map((message) => row(session, message));
    sessions.set(session.id, session);
    return session;
  };
  samples.forEach(create);
  if (selectedSession() && !sessions.has(selectedSession())) rememberSession(null, true);
  const resources = {
    profiles: { profiles: [profile] }, readiness,
    capabilities: { features: { session_resources: true, session_fork: true, run_submission: true, run_stop: true, run_steer: true, model_options: true },
      talaria_agent: { name: "Hermes" }, talaria_extensions: extensions },
    agent: { name: "Hermes", version: "0.3.0", status: "ok", gateway_state: "running",
      platforms: [{ name: "api_server", state: "connected" }, { name: "discord", state: "connected" }],
      extended_access: extensions, readiness },
    models: { model, provider, configured_reasoning: "high", providers: [
      { id: provider, name: "Anthropic", authenticated: true, is_current: true, models: [model] },
    ] },
    connection: { url: profile.server_url, profile: "default", key_set: true },
    commands: { commands: [
      ["help", "List commands"], ["new", "Start a new session"], ["model", "Choose a model"],
      ["reasoning", "Choose reasoning"], ["title", "Rename this session"], ["branch", "Branch this session"],
      ["status", "Session details"], ["save", "Download transcript"],
      ["compress", "Compress session context"], ["version", "Hermes version"], ["profile", "Current profile"],
      ["egress", "Network access policy"], ["bundles", "Enabled bundles"],
    ].map(([name, description]) => ({ name, description, aliases: name === "help" ? ["commands"] : [], args: "",
      mode: ["compress", "version", "profile", "egress", "bundles"].includes(name) ? "native" : "web" })) }, activity: { data: [], has_more: false },
  };
  function missing() { throw new RequestError(msg("Not found."), 404, "not_found"); }
  function complete(run, cancelled = false) {
    if (run.status !== "running") return;
    run.status = cancelled ? "cancelled" : "completed";
    run.session.messages.push(...(cancelled
      ? [row(run.session, { role: "assistant", content: run.output, finish_reason: "cancelled" })]
      : run.reply.map((message) => row(run.session, message))));
    run.session.updated_at = Date.now() / 1000;
  }
  function submit(body) {
    const session = sessions.get(body.session_id);
    if (!session) return missing();
    const previous = [...runs.values()].find((run) => run.request_id === body.request_id);
    if (previous) return { run_id: previous.id };
    const source = samples.find((sample) => sample.id === session.id) || samples[1];
    const lastUser = source.messages.findLastIndex((message) => message.role === "user");
    const reply = structuredClone(source.messages.slice(lastUser + 1));
    const identity = randomId();
    for (const message of reply) {
      if (message.tool_call_id) message.tool_call_id = identity + message.tool_call_id;
      for (const call of message.tool_calls || []) call.id = identity + call.id;
    }
    const run = { id: randomId(), request_id: body.request_id, session, reply,
      status: "running", output: "", cursor: 0, events: [] };
    session.messages.push(row(session, { role: "user", content: body.images?.length
      ? [{ type: "text", text: body.input }, ...body.images.map((image) => ({ type: "image_url", image_url: image }))]
      : body.input }));
    for (const [index, message] of reply.entries()) {
      if (message.role !== "assistant") continue;
      for (const [field, event] of [["reasoning", "talaria.reasoning.delta"], ["content", "message.delta"]]) {
        const text = message[field] || "";
        for (let i = 0; i < text.length; i += 32)
          run.events.push({ event, delta: text.slice(i, i + 32), block_id: String(index) });
      }
      for (const call of message.tool_calls || []) run.events.push(
        { event: "tool.started", tool_call_id: call.id, tool: call.function.name, preview: call.function.arguments },
        { event: "tool.completed", tool_call_id: call.id, duration: 0.6 },
      );
    }
    runs.set(run.id, run);
    // Bound disposable simulation state in long-lived demo tabs.
    for (const [id, old] of runs) if (runs.size > 16 && old.status !== "running") runs.delete(id);
    return { run_id: run.id };
  }
  function changeSession(session, action, method, body) {
    if (method === "PATCH" && !action) {
      if (typeof body.title === "string") session.title = body.title;
      if (typeof body.pinned === "boolean") session.pinned = body.pinned;
      return summary(session);
    }
    if (method === "DELETE" && !action) { sessions.delete(session.id); return {}; }
    if (method === "POST" && action === "fork") {
      let title = body.title || session.title, number = 1;
      while ([...sessions.values()].some((item) => item.title === title)) title = `${body.title || session.title} #${++number}`;
      return summary(create({ ...session, id: randomId(), title, source: "api_server", pinned: false,
        parent_session_id: session.id, model_config: { _branched_from: session.id },
        messages: structuredClone(session.messages) }));
    }
    if (method === "POST" && action === "rewind") {
      const index = session.messages.findIndex((message) => message.id === body.message_id);
      const start = session.messages.findLastIndex((message, i) => i <= index && message.role === "user");
      if (start < 0) return missing();
      const revision = JSON.stringify(session.messages.map((message) => message.id));
      if (body.preview) return { revision, user: structuredClone(session.messages[start]),
        target_id: session.messages[start].id, message_count: session.messages.length - start,
        turn_count: session.messages.slice(start).filter((message) => message.role === "user").length };
      if (body.revision !== revision) throw new RequestError(msg("The session has changed. Reopen this action to try again."), 409, "session_changed");
      session.messages = session.messages.slice(0, start);
      return {};
    }
    throw new RequestError(msg("The demo is read-only."), 405, "demo_read_only");
  }
  function request(path, { method = "GET", body = {}, signal } = {}) {
    signal?.throwIfAborted();
    const url = new URL(path, location.origin), parts = url.pathname.split("/").filter(Boolean);
    if (parts[0] === "commands" && method === "POST") {
      const text = { compress: "Context is already compact.", version: "Hermes Agent 0.3.0", profile: "default",
        egress: "No outbound network access.", bundles: "No bundles enabled." }[body.command];
      if (!text) return missing();
      jobs.set(body.request_id, { status: "completed", text, session_id: body.session_id });
      if (jobs.size > 16) jobs.delete(jobs.keys().next().value);
      return { id: body.request_id };
    }
    if (parts[0] === "commands" && parts[1]) return jobs.get(parts[1]) || missing();
    if (method === "GET" && Object.hasOwn(resources, parts[0])) return structuredClone(resources[parts[0]]);
    if (method === "GET" && parts[0] === "installation") return {
      version,
      managed: true, branch: "stable", can_update: true,
      update: { available: false, checked_at: new Date().toISOString() },
    };
    if (parts[0] === "sessions" && parts.length === 1) {
      if (method === "GET") return { data: [...sessions.values()].toSorted((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || b.updated_at - a.updated_at).map(summary), has_more: false };
      if (method === "POST") return summary(create({ id: randomId(), title: body.title }));
    }
    if (parts[0] === "runs") {
      if (method === "POST" && parts.length === 1) return submit(body);
      const run = runs.get(parts[1]);
      if (!run) return missing();
      if (method === "GET") return { status: run.status, output: run.output };
      if (method === "POST" && parts[2] === "stop") { complete(run, true); return {}; }
      if (method === "POST" && parts[2] === "steer") return {};
    }
    if (method !== "GET") {
      const session = parts[0] === "sessions" && sessions.get(parts[1]);
      if (session) return changeSession(session, parts[2], method, body);
      throw new RequestError(msg("The demo is read-only."), 405, "demo_read_only");
    }
    if (parts[0] === "search") {
      const needle = (url.searchParams.get("q") || "").trim().toLowerCase();
      return { data: [...sessions.values()].flatMap((session) => session.messages
        .filter((message) => needle && plainContent(message.content).toLowerCase().includes(needle))
        .map((message) => {
          const content = plainContent(message.content);
          const pos = content.toLowerCase().indexOf(needle);
          return { ...message, title: session.title, source: session.source,
            snippet: content.slice(Math.max(0, pos - 80), pos) + ">>>" +
              content.slice(pos, pos + needle.length) + "<<<" + content.slice(pos + needle.length, pos + needle.length + 160) };
        })), has_more: false };
    }
    const session = sessions.get(parts[1]);
    if (parts[0] !== "sessions" || !session) return missing();
    if (parts.length === 2) return summary(session);
    if (["messages", "around"].includes(parts[2])) {
      const end = session.messages.length - Math.max(0, Number(url.searchParams.get("offset")) || 0);
      const limit = Math.max(1, Math.min(500, Number(url.searchParams.get("limit")) || 100));
      const data = end > 0 ? session.messages.slice(Math.max(0, end - limit), end) : [];
      return { session_id: session.id, data: structuredClone(data), has_more: end > limit,
        next_offset: session.messages.length - end + data.length };
    }
    if (parts[2] === "export") return url.searchParams.get("format") === "json"
      ? { session: summary(session), messages: session.messages }
      : session.messages.map((message) => `## ${message.role === "user" ? "You" : message.name || "Hermes"}\n\n${plainContent(message.content)}`).join("\n\n");
    if (parts[2] === "context") return { context: { used: 9300, maximum: 200000 } };
    if (parts[2] === "response") return { model, provider, reasoning: "high", has_reasoning: true,
      duration_seconds: 2.4, finish_reason: "stop", usage: { input_tokens: 9300, output_tokens: 840 } };
    return missing();
  }
  function events(path) {
    const run = runs.get(path.split("/")[2]);
    const source = { onmessage: null, onopen: null, close: () => clearTimeout(timer) };
    let timer;
    function next() {
      if (!run) return source.onmessage?.({ data: JSON.stringify({ event: "run.interrupted" }) });
      let event = run.status === "cancelled" ? { event: "run.cancelled", output: run.output }
        : run.events[run.cursor++];
      if (!event) { complete(run); event = { event: "run.completed", output: run.reply.at(-1).content }; }
      if (event.event === "message.delta") run.output += event.delta;
      source.onmessage?.({ data: JSON.stringify(event) });
      if (run.status === "running") timer = setTimeout(next, event.event.startsWith("tool.") ? 400 : 45);
    }
    timer = setTimeout(() => { source.onopen?.(); next(); }, 0);
    return source;
  }
  setTransport({ request: async (path, options = {}) => {
    try {
      const data = request(path, { ...options, body: options.body ? JSON.parse(options.body) : {} });
      return typeof data === "string" ? new Response(data) : Response.json(data);
    } catch (error) {
      if (!(error instanceof RequestError)) throw error;
      return Response.json({ error: error.message, code: error.code }, { status: error.status });
    }
  }, events });
  enabled = true;
}
