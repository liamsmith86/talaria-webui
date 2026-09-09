import {
  html,
  useEffect,
  useRef,
  useState,
  useMemo,
  Icon,
  IconButton,
  humanTime,
} from "./lib.js";
import { Markdown } from "./markdown.js";
import { approve, retrySubmission } from "./runs.js";
import { toast, fail, supports, loadOlderMessages } from "./store.js";

export function plainContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .map(
        (p) => p.text || (p.type === "image_url" ? "[Image attachment]" : ""),
      )
      .filter(Boolean)
      .join("\n");
  return "";
}

function ToolCard({ tool }) {
  const isAgent = tool.kind === "agent";
  const icon = isAgent
    ? "branch"
    : /search|browse|web/.test(tool.name)
      ? "globe"
      : /file|read|write/.test(tool.name)
        ? "file"
        : "terminal";
  return html`<details
    class=${`tool-card ${tool.status === "running" ? "working" : ""}`}
  >
    <summary>
      <span class="tool-icon"><${Icon} name=${icon} size=${17} /></span
      ><span class="tool-label"
        >${isAgent
          ? "Subagent"
          : (tool.name || "Tool").replace(/_/g, " ")}<small
          >${isAgent
            ? tool.name
            : tool.preview?.split("\n")[0]?.slice(0, 110) ||
              (tool.status === "running" ? "Working…" : "Finished")}</small
        ></span
      ><span class="tool-status"
        >${tool.status === "running"
          ? html`<span class="spinner" />`
          : html`<${Icon}
              name=${tool.status === "failed" ? "alert" : "check"}
              size=${15}
            />`}</span
      ><${Icon} name="chevron" size=${14} />
    </summary>
    <div class="tool-content">
      ${tool.preview
        ? html`<pre>${tool.preview}</pre>`
        : html`<p>
            ${tool.status === "running"
              ? "Hermes is using this tool."
              : "This tool has finished."}
          </p>`}${tool.duration !== undefined &&
      html`<small>${tool.duration}s</small>`}
    </div>
  </details>`;
}

function Message({
  role,
  text,
  time,
  tools = [],
  streaming = false,
  reasoning = "",
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      toast("Select the message text to copy it.");
    }
  }
  return html`<article
    class=${`message ${role}`}
    aria-label=${role === "user" ? "Your message" : "Hermes response"}
  >
    ${role !== "user" &&
    html`<div class="message-byline">
      <span class="agent-avatar"><${Icon} name="spark" size=${14} /></span
      ><span>Hermes</span>${time && html`<time>${humanTime(time)}</time>`}
    </div>`}
    ${reasoning &&
    html`<details class="reasoning">
      <summary>Thinking<${Icon} name="chevron" size=${14} /></summary>
      <${Markdown} text=${reasoning} />
    </details>`}
    ${tools.length > 0 &&
    html`<div class="tool-stack">
      ${tools.map((tool) => html`<${ToolCard} key=${tool.id} tool=${tool} />`)}
    </div>`}
    ${text &&
    (role === "user"
      ? html`<div class="user-content">${text}</div>`
      : html`<${Markdown} text=${text} streaming=${streaming} />`)}
    ${streaming &&
    !text &&
    html`<div class="thinking" role="status">
      <span /><span /><span /><span class="sr-only">Hermes is thinking</span>
    </div>`}
    ${!streaming &&
    text &&
    html`<div class="message-actions">
      <${IconButton}
        name=${copied ? "check" : "copy"}
        label=${copied ? "Copied" : "Copy message"}
        onClick=${copy}
      />
    </div>`}
  </article>`;
}

function Approval({ sid, request }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function respond(choice) {
    setBusy(true);
    try {
      await approve(sid, choice);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const choices = request.choices || ["once", "deny"];
  return html`<section class="approval-card" aria-label="Approval required">
    <div class="approval-heading">
      <${Icon} name="alert" size=${18} /><strong
        >A moment for your approval</strong
      >
    </div>
    <p>Hermes would like to run this command.</p>
    <pre>
${request.command ||
      request.description ||
      request.preview ||
      "A tool needs your permission to continue."}</pre
    >
    ${error && html`<div class="form-error" role="alert">${error}</div>`}
    <div class="approval-actions">
      <button
        class="button secondary"
        disabled=${busy}
        onClick=${() => respond("deny")}
      >
        Deny</button
      >${choices.includes("session") &&
      html`<button
        class="button secondary"
        disabled=${busy}
        onClick=${() => respond("session")}
      >
        Allow for session
      </button>`}<button
        class="button primary"
        disabled=${busy || !supports("run_approval_response")}
        onClick=${() => respond("once")}
      >
        ${busy ? "Sending…" : "Allow once"}
      </button>
    </div>
  </section>`;
}

function historyItems(history) {
  const items = [];
  const calls = new Map();
  for (const message of history) {
    if (message.role === "tool") {
      const call = calls.get(message.tool_call_id);
      if (call) {
        call.preview = plainContent(message.content).slice(0, 20000);
        call.status = "completed";
      }
    } else if (message.role === "user" || message.role === "assistant") {
      const tools = (message.tool_calls || []).map((t, i) => ({
        id: t.id || i,
        name: t.function?.name || t.name,
        preview:
          typeof t.function?.arguments === "string"
            ? t.function.arguments
            : JSON.stringify(t.function?.arguments || {}),
        status: "completed",
      }));
      for (const tool of tools) calls.set(tool.id, tool);
      items.push({
        role: message.role,
        text: plainContent(message.content),
        tools,
        time: message.timestamp || message.created_at,
        reasoning: message.reasoning || message.reasoning_content || "",
        id: message.id || message.row_id || items.length,
      });
    }
  }
  return items.filter((m) => m.text || m.tools.length);
}

export function Conversation({ app }) {
  const scroll = useRef();
  const bottom = useRef();
  const sticky = useRef(true);
  const [away, setAway] = useState(false);
  const [olderBusy, setOlderBusy] = useState(false);
  const items = useMemo(() => historyItems(app.history), [app.history]);
  const live = app.lives[app.active];
  const streaming =
    live &&
    !["completed", "failed", "cancelled", "interrupted"].includes(live.status);
  useEffect(() => {
    sticky.current = true;
    setAway(false);
    bottom.current?.scrollIntoView();
  }, [app.active]);
  useEffect(() => {
    if (sticky.current) bottom.current?.scrollIntoView({ behavior: "instant" });
  }, [app.history, live?.text, live?.tools.length, live?.approval]);
  const lastUser = [...items].reverse().find((m) => m.role === "user");
  const showUser = live && !live.persisted && lastUser?.text !== live.userText;
  return html`<div
    class="conversation-viewport"
    ref=${scroll}
    onScroll=${(e) => {
      const el = e.currentTarget;
      sticky.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      setAway(!sticky.current);
    }}
  >
    <div class="conversation-content">
      ${!app.loading &&
      app.historyHasMore &&
      html`<button
        class="load-more"
        disabled=${olderBusy}
        onClick=${async () => {
          const el = scroll.current,
            previousHeight = el.scrollHeight,
            previousTop = el.scrollTop;
          sticky.current = false;
          setOlderBusy(true);
          try {
            await loadOlderMessages();
            requestAnimationFrame(() => {
              el.scrollTop = previousTop + el.scrollHeight - previousHeight;
            });
          } catch (e) {
            fail(e);
          } finally {
            setOlderBusy(false);
          }
        }}
      >
        ${olderBusy ? "Loading…" : "Load earlier messages"}
      </button>`}
      ${app.loading &&
      html`<div class="history-loading" role="status">
        <span class="skeleton long" /><span class="skeleton" /><span
          class="skeleton medium"
        /><span class="sr-only">Loading conversation</span>
      </div>`}
      ${!app.loading &&
      items.map((m) => html`<${Message} key=${m.id} ...${m} />`)}
      ${showUser && html`<${Message} role="user" text=${live.userText} />`}
      ${live &&
      !live.persisted &&
      html`<${Message}
        role="assistant"
        text=${live.text}
        tools=${live.tools}
        streaming=${streaming}
        reasoning=${live.reasoning}
      />`}
      ${live?.approval &&
      html`<${Approval}
        key=${live.approval.request_id || live.id}
        sid=${app.active}
        request=${live.approval}
      />`}
      ${live?.reconnecting &&
      html`<div class="run-notice" role="status">
        <span class="spinner" /> Reconnecting to live updates. Hermes is still
        working.
      </div>`}
      ${live?.error &&
      html`<div class="run-error" role="alert">
        ${live.error}${live.submissionFailed &&
        html`<button
          class="text-button"
          onClick=${() => retrySubmission(app.active).catch(fail)}
        >
          Retry submission
        </button>`}
      </div>`}
      ${live?.status === "cancelled" &&
      html`<div class="run-notice">You stopped this response.</div>`}
      ${live?.pendingSteer &&
      html`<div class="run-notice">
        Guidance arrived after this response: ${live.pendingSteer}
      </div>`}
      <div ref=${bottom} class="scroll-anchor" />
    </div>
    ${away &&
    html`<button
      class="jump-bottom"
      aria-label="Jump to latest message"
      onClick=${() => {
        sticky.current = true;
        bottom.current?.scrollIntoView({ behavior: "smooth" });
      }}
    >
      <${Icon} name="down" size=${18} />
    </button>`}
  </div>`;
}
