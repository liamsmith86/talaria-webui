// Hermes activity between the agent's prose: one-line tool and reasoning
// steps, folded into a single group when several tools run in a row.
import { html, useMemo, useState, Icon, humanTime } from "./lib.js";
import { Markdown } from "./markdown.js";
import { ToolCode } from "./tool-code.js";
import { ToolResult } from "./tool-result.js";
import { duration, money, numberValue } from "./content.js";
import { state, openSession } from "./store.js";
import { t, msg, n, rich, formatList } from "./i18n.js";

const statuses = {
  running: msg("Running"),
  completed: msg("Completed"),
  finished: msg("Finished"),
  failed: msg("Failed"),
  error: msg("Error"),
  cancelled: msg("Cancelled"),
  interrupted: msg("Interrupted"),
  timeout: msg("Timed out"),
  timed_out: msg("Timed out"),
  not_reported: msg("Not reported"),
};
const failures = new Set(["failed", "error", "cancelled", "interrupted", "timeout", "timed_out"]);
const toolFailed = (tool) => failures.has(tool.status);

function statusText(status) {
  if (Object.hasOwn(statuses, status)) return t(statuses[status]);
  return typeof status === "string" ? status.replace(/_/g, " ") : t("Finished");
}

// Hermes's primary display argument for its core tools (agent/display.py).
// Typed browser text is deliberately absent: it can contain credentials.
const primaryArgs = {
  terminal: "command", web_search: "query", web_extract: "urls", read_file: "path",
  write_file: "path", patch: "path", search_files: "pattern", browser_navigate: "url",
  browser_click: "ref", image_generate: "prompt", text_to_speech: "text",
  vision_analyze: "question", skill_view: "name", skills_list: "category",
  cronjob_manage: "action", execute_code: "code", browser_exec: "code",
  delegate_task: "goal", skill_manage: "name",
};
const fallbackArgs = ["query", "text", "command", "path", "name", "prompt", "code", "goal"];
// Tool names come from plugins and MCP servers: never resolve "constructor"
// or "toString" through a lookup table's prototype.
const own = (table, key) => (Object.hasOwn(table, key) ? table[key] : undefined);

// One parse of a saved call's arguments: a one-line subject for its row and
// the readable input shown when the row is expanded.
export function describeCall(name, args) {
  let value;
  try {
    value = JSON.parse(args);
  } catch {
    return { subject: "", input: args };
  }
  if (!value || typeof value !== "object" || Array.isArray(value))
    return { subject: "", input: args };
  const key = name === "browser_type" ? null
    : own(primaryArgs, name) || fallbackArgs.find((field) => Object.hasOwn(value, field));
  const primary = Array.isArray(value[key]) ? value[key][0] : value[key];
  const subject = ["string", "number"].includes(typeof primary)
    ? String(primary).replace(/\s+/g, " ").trim().slice(0, 300) : "";
  // Show code and lone text arguments as written, not as escaped JSON.
  const keys = Object.keys(value);
  const code = ["terminal", "execute_code"].includes(name) ? value[primaryArgs[name]] : null;
  const input = typeof code === "string" ? code
    : keys.length === 1 && typeof value[keys[0]] === "string" ? value[keys[0]]
      : JSON.stringify(value, null, 2);
  return { subject, input };
}

function parsedResult(output) {
  if (output.length > 250000 || !output.startsWith("{")) return null;
  try {
    return JSON.parse(output);
  } catch {
    return null;
  }
}

export function terminalExit(output) {
  const code = parsedResult(output)?.exit_code;
  return Number.isInteger(code) ? code : null;
}

const fileMutationLanded = {
  write_file: (result) => "bytes_written" in result,
  patch: (result) => result.success === true,
};
const failureMarker = /"(?:error|failed)"(?!\s*:\s*(?:null|false|0|""|\[\]|\{\})\s*(?:[,}\]]|$))/gi;

function batchFailed(items) {
  return Array.isArray(items) && items.length > 0 &&
    items.every((item) => item && typeof item === "object" && item.error);
}

// Saved rows carry no outcome. Hermes reports a live call's from its result, by
// these rules (agent/display.py), so a reload has to reach the same verdict.
export function resultFailed(name, output) {
  const result = parsedResult(output);
  if (!result) {
    if (name === "terminal" || output.length > 250000) return false;
    const marked = [...output.slice(0, 600).matchAll(failureMarker)].some((m) => m.index < 500);
    return marked || output.startsWith("Error");
  }
  if (!result.error && fileMutationLanded[name]?.(result)) return false;
  if (result.guardrail_refusal === true) return false;
  if (result.user_summary) return true;
  if (name === "terminal") return !!terminalExit(output);
  if ((result.error || result.message) && (result.success === false || "error" in result)) return true;
  return ["failed", "error"].includes(String(result.status).toLowerCase()) || batchFailed(result.results);
}

// Labels follow Hermes's curated verbs; each pair is [finished, in progress].
const labels = {
  terminal: [msg("Ran {subject}"), msg("Running {subject}")],
  execute_code: [msg("Ran code"), msg("Running code")],
  read_file: [msg("Read {subject}"), msg("Reading {subject}")],
  web_extract: [msg("Read {subject}"), msg("Reading {subject}")],
  write_file: [msg("Wrote {subject}"), msg("Writing {subject}")],
  patch: [msg("Edited {subject}"), msg("Editing {subject}")],
  search_files: [msg("Searched files for {subject}"), msg("Searching files for {subject}")],
  web_search: [msg("Searched the web for {subject}"), msg("Searching the web for {subject}")],
  session_search: [msg("Searched past sessions"), msg("Searching past sessions")],
  browser_navigate: [msg("Opened {subject}"), msg("Opening {subject}")],
  browser_click: [msg("Clicked {subject}"), msg("Clicking {subject}")],
  browser_type: [msg("Typed text"), msg("Typing text")],
  vision_analyze: [msg("Looked at an image"), msg("Looking at an image")],
  image_generate: [msg("Generated an image"), msg("Generating an image")],
  text_to_speech: [msg("Generated speech"), msg("Generating speech")],
  skill_view: [msg("Read skill {subject}"), msg("Reading skill {subject}")],
  skills_list: [msg("Listed skills"), msg("Listing skills")],
  memory: [msg("Updated memory"), msg("Updating memory")],
  todo_list: [msg("Updated tasks"), msg("Updating tasks")],
  delegate_task: [msg("Delegated a task"), msg("Delegating a task")],
  clarify: [msg("Asked a question"), msg("Asking a question")],
};
const categories = {
  terminal: "command", execute_code: "command",
  read_file: "read",
  write_file: "edit", patch: "edit",
  web_search: "search", search_files: "search", session_search: "search",
  web_extract: "web", browser_navigate: "web",
};
const icons = { command: "terminal", read: "file", edit: "edit", search: "search", web: "globe" };
const category = (tool) => own(categories, tool.name) || "other";
const toolIcon = (tool) => icons[category(tool)] || (tool.name === "delegate_task" ? "branch"
  : /^(?:browser|web)_/.test(tool.name) ? "globe" : "settings");
const phrases = {
  command: (count) => n("ran {count} command", "ran {count} commands", count),
  read: (count) => n("read {count} file", "read {count} files", count),
  edit: (count) => n("edited {count} file", "edited {count} files", count),
  search: (count) => n("searched {count} time", "searched {count} times", count),
  web: (count) => n("visited {count} page", "visited {count} pages", count),
  other: (count) => n("used {count} tool", "used {count} tools", count),
};

const segment = (text) => text && html`<span class="tool-verb">${text}</span>`;
// Hermes reports most file and search calls in milliseconds; "0s" is noise.
const elapsed = (seconds) => (numberValue(seconds) >= 0.05 ? duration(seconds) : "");

// Keep the verb visible and let only the subject shorten, wherever a
// translation places it.
function toolLabel(tool, active) {
  const pair = own(labels, tool.name);
  const text = tool.subject || "";
  if (pair && (text || !pair[0].includes("{subject}")))
    return rich(pair[active ? 1 : 0], {
      subject: html`<span class="tool-subject">${text}</span>`,
    }).map((part) => (typeof part === "string" ? segment(part) : part));
  const name = (tool.name || t("Tool")).replace(/_/g, " ");
  return [segment(`${name[0].toLocaleUpperCase()}${name.slice(1)}${text ? " " : ""}`),
    text && html`<span class="tool-subject">${text}</span>`];
}

function ToolStatus({ tool }) {
  const label = statusText(tool.status);
  if (tool.status === "running")
    return html`<span class="tool-status"><span class="spinner" /><span class="sr-only">${label}</span></span>`;
  if (toolFailed(tool))
    return html`<span class="tool-status failed"><${Icon} name="alert" size=${13} />${label}</span>`;
  if (tool.status === "completed")
    return html`<span class="tool-status"><${Icon} name="check" size=${13} /><span class="sr-only">${label}</span></span>`;
  return html`<span class="tool-status" title=${t("Outcome not reported by Hermes")}
    ><span class="sr-only">${label}</span></span>`;
}

export function ToolRow({ tool, matched = false, initiallyOpen = false, onToggle }) {
  const [open, setOpen] = useState(initiallyOpen);
  // A path or query already shown in the row needs no second copy. Commands
  // keep their block for copying and highlighting.
  const input = tool.input ?? tool.subject;
  const details = input && (input !== tool.subject || tool.name === "terminal");
  const meta = [
    tool.exitCode != null && tool.exitCode !== 0 && t("exit {code}", { code: tool.exitCode }),
    elapsed(tool.duration),
  ].filter(Boolean).join(" · ");
  return html`<details
    class=${`tool-card${tool.status === "running" ? " working" : ""}${matched ? " search-tool-match" : ""}`}
    data-search-target=${matched ? "true" : undefined}
    open=${initiallyOpen}
    onToggle=${(event) => {
      setOpen(event.currentTarget.open);
      onToggle?.(event.currentTarget.open);
    }}
  >
    <summary>
      <span class="tool-icon"><${Icon} name=${toolIcon(tool)} size=${14} /></span
      ><span class=${`tool-label${details ? "" : " findable"}`}>${toolLabel(tool, tool.status === "running")}</span
      >${meta && html`<span class="tool-meta">${meta}</span>`}<${ToolStatus} tool=${tool}
      /><${Icon} name="chevron" size=${14} />
    </summary>
    <div class="tool-content">
      ${details &&
      html`<${ToolCode} text=${input} label=${t("Tool details")}
        shell=${tool.name === "terminal"} enabled=${open} />`}
      ${tool.output !== undefined
        ? html`<small>${t("Result")}</small>
            <${ToolResult} text=${tool.output}
              enabled=${open && tool.status !== "running"} />`
        : !details && html`<p>
            ${tool.status === "running"
              ? t("Hermes is using this tool.")
              : t("This tool has finished.")}
          </p>`}
    </div>
  </details>`;
}

// The first prose line of a reasoning block, without its Markdown syntax.
function thoughtPreview(text) {
  for (const line of text.slice(0, 800).split("\n")) {
    const plain = line
      .replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)/, "")
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\*\*|__|~~|[`*]/g, "")
      .trim();
    if (plain) return plain;
  }
  return "";
}

export function ThoughtRow({ text, streaming = false, deferHighlight = false, initiallyOpen = false, onToggle }) {
  const preview = useMemo(() => thoughtPreview(text), [text]);
  return html`<details class=${`reasoning${streaming ? " working" : ""}`} open=${initiallyOpen}
    onToggle=${(event) => onToggle?.(event.currentTarget.open)}>
    <summary>
      <span class="tool-icon"><${Icon} name="thought" size=${14} /></span
      ><span class="tool-label">${segment(`${streaming ? t("Thinking") : t("Thought")}${preview ? " " : ""}`)}${preview &&
        html`<span class="tool-subject">${preview}</span>`}</span
      ><${Icon} name="chevron" size=${14} />
    </summary>
    <${Markdown} text=${text} streaming=${streaming} deferHighlight=${deferHighlight} />
  </details>`;
}

// Files and pages are counted once however often they are used.
function tally(tools) {
  const counts = new Map();
  const seen = new Set();
  for (const tool of tools) {
    const kind = category(tool);
    const identity = ["read", "edit", "web"].includes(kind) && tool.subject
      ? `${kind}:${tool.subject}` : tool;
    if (seen.has(identity)) continue;
    seen.add(identity);
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  return counts;
}

function summary(counts) {
  const text = formatList(Object.keys(phrases).filter((key) => counts.has(key))
    .map((key) => phrases[key](counts.get(key))));
  return segment(text.charAt(0).toLocaleUpperCase() + text.slice(1));
}

// Hermes runs read-only tools in parallel, so durations do not add up: use
// the span from the first start to the last finish Hermes reported.
function span(tools) {
  const starts = tools.map((tool) => numberValue(tool.startedAt));
  const ends = tools.map((tool) => numberValue(tool.endedAt));
  if (starts.includes(null) || ends.includes(null)) return "";
  return elapsed(Math.max(...ends) - Math.min(...starts));
}

// Several consecutive steps. Folded by default; while it is the reply's
// current activity its header follows the latest step.
export function ActivityGroup({ steps, working, initiallyOpen = false, onToggle }) {
  const tools = steps.flatMap((step) => (step.tool ? [step.tool] : []));
  const last = steps.at(-1);
  const failed = tools.filter(toolFailed).length;
  const total = span(tools);
  const counts = tally(tools);
  // The most frequent kind of step represents the group.
  const main = tools.find((tool) => category(tool) ===
    [...counts].sort((a, b) => b[1] - a[1])[0][0]);
  const title = working && last.tool?.status === "running" ? toolLabel(last.tool, true)
    : working && !last.tool ? segment(t("Thinking")) : summary(counts);
  return html`<details class=${`activity${working ? " working" : ""}`} open=${initiallyOpen}
    onToggle=${(event) => onToggle?.(event.currentTarget.open)}>
    <summary>
      <span class="tool-icon">${working
        ? html`<span class="spinner" />`
        : html`<${Icon} name=${toolIcon(main)} size=${14} />`}</span
      ><span class="tool-label">${title}</span
      >${!working && total && html`<span class="tool-meta">${total}</span>`}${failed > 0 &&
      html`<span class="activity-failed">${n("{count} failed", "{count} failed", failed)}</span>`}<${Icon}
        name="chevron" size=${14} />
    </summary>
    <ol class="activity-steps">
      ${steps.map((step) => html`<li key=${step.key}>${step.node}</li>`)}
    </ol>
  </details>`;
}

// Group consecutive tool and reasoning parts between prose, images and
// subagents. Groups need at least two tools; other steps stay single rows.
export function activityRuns(parts) {
  const segments = [];
  let run = null;
  for (const part of parts) {
    if (part.kind === "reasoning" || (part.kind === "tool" && !part.agent)) {
      if (!run) segments.push((run = { steps: [], tools: 0 }));
      run.steps.push(part);
      if (part.kind === "tool") run.tools++;
    } else {
      run = null;
      segments.push(part);
    }
  }
  return segments.flatMap((item) => (item.steps && item.tools < 2 ? item.steps : [item]));
}

// Guidance the user sent while Hermes worked, where the model received it.
export function GuidanceRow({ text, matched = false }) {
  return html`<div class="guidance" data-search-target=${matched ? "true" : undefined}>
    <span class="tool-icon"><${Icon} name="message" size=${14} /></span
    ><p><span class="tool-verb">${t("Your guidance")}</span> <span class="guidance-text">${text}</span></p>
  </div>`;
}

// Hermes's CLI replays its events (content.js NOTICE_KINDS) as one-line
// notices, never as something the user wrote. A compaction Talaria ran is one
// too: Hermes hides compaction from its transcript, and Talaria records it.
const notices = {
  async_delegation_complete: [msg("Background delegation completed"), "branch"],
  process_complete: [msg("Background process finished"), "terminal"],
  auto_continue: [msg("Resumed an interrupted turn"), "refresh"],
  model_switch: [msg("Model changed"), "spark"],
  compaction: [msg("Context compressed"), "context"],
};
export function Notice({ kind, text, time, matched = false }) {
  const [label, icon] = notices[kind];
  return html`<article class=${`message notice${matched ? " search-match" : ""}`}>
    <details>
      <summary>
        <span class="tool-icon"><${Icon} name=${icon} size=${14} /></span
        ><span class="tool-label">${segment(t(label))}</span
        >${time && html`<time class="tool-meta">${humanTime(time)}</time>`}<${Icon} name="chevron" size=${14} />
      </summary>
      <${Markdown} text=${text} />
    </details>
  </article>`;
}

export function AgentCard({ tool }) {
  const [open, setOpen] = useState(false);
  return html`<details
    class=${`tool-card agent-card${tool.status === "running" ? " working" : ""}`}
    onToggle=${(event) => setOpen(event.currentTarget.open)}
  >
    <summary>
      <span class="tool-icon"><${Icon} name="branch" size=${15} /></span
      ><span class="tool-label"
        >${t("Subagent")}<small>${tool.name || t("Delegated task")}</small></span
      ><${ToolStatus} tool=${tool} /><${Icon} name="chevron" size=${14} />
    </summary>
    <div class="tool-content">
      ${tool.summary
        ? html`<${ToolCode} text=${tool.summary} label=${t("Tool details")} enabled=${open} />`
        : html`<p>
            ${tool.status === "running"
              ? t("Hermes is using this tool.")
              : t("This tool has finished.")}
          </p>`}
      <div class="child-facts">
        ${typeof tool.model === "string" &&
        html`<span>${tool.model}</span>`}${duration(tool.duration_seconds) &&
        html`<span>${duration(tool.duration_seconds)}</span>`}${numberValue(
          tool.cost_usd,
        ) !== null &&
        html`<span title=${t("Cost reported by Hermes")}
          >${money(tool.cost_usd)} USD</span
        >`}
      </div>
      ${typeof tool.child_session_id === "string" &&
      html`<button
        class="text-button child-link"
        onClick=${() =>
          openSession(tool.child_session_id, {
            id: state.active,
            title:
              state.sessionDetails?.title ||
              state.sessions.find((s) => s.id === state.active)?.title ||
              t("Parent session"),
          })}
      >
        ${t("Open child session")}<${Icon} name="link" size=${14} />
      </button>`}
      ${tool.status !== "running" &&
      !tool.child_session_id &&
      html`<p class="field-help">
        ${t("Hermes did not include a child transcript link.")}
      </p>`}
    </div>
  </details>`;
}
