import {
  html,
  useEffect,
  useRef,
  useState,
  useMemo,
  useCallback,
  Icon,
  IconButton,
  humanTime,
} from "./lib.js";
import { CommandCard, commandRunning } from "./command-activity.js";
import { Markdown } from "./markdown.js";
import { approve, retrySubmission, running } from "./runs.js";
import {
  state,
  toast,
  fail,
  supports,
  loadOlderMessages,
  openSession,
  update,
} from "./store.js";
import { plainContent, duration, money, numberValue } from "./content.js";
import {
  currentImageMessage,
  messageImages,
  withoutImagePlaceholders,
} from "./attachments.js";
import { Images } from "./images.js";
import { ConversationFind } from "./conversation-find.js";
import { responseParts } from "./response-parts.js";

function toolText(value, input = false) {
  if (typeof value !== "string") return "";
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object") return value;
    const keys = input
      ? ["command", "path", "query", "url"]
      : ["output", "content", "text"];
    for (const key of keys)
      if (typeof parsed[key] === "string") return parsed[key];
    return JSON.stringify(parsed, null, 2);
  } catch {
    return value;
  }
}

function ToolCard({ tool }) {
  const isAgent = tool.kind === "agent";
  const failed = [
    "failed",
    "error",
    "cancelled",
    "interrupted",
    "timeout",
    "timed_out",
  ].includes(tool.status);
  const preview = useMemo(
    () => toolText(tool.preview, !isAgent),
    [tool.preview, isAgent],
  );
  const output = useMemo(() => toolText(tool.output), [tool.output]);
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
            : preview.split("\n")[0]?.slice(0, 110) ||
              (tool.status === "running" ? "Working…" : "Finished")}</small
        ></span
      ><span class="tool-status"
        >${tool.status === "running"
          ? html`<span class="spinner" />`
          : html`<${Icon}
              name=${failed ? "alert" : tool.status === "not_reported" ? "info" : "check"}
              size=${15}
            />`}<span class="sr-only"
          >${typeof tool.status === "string"
            ? tool.status.replace(/_/g, " ")
            : "Finished"}</span
        ></span
      ><${Icon} name="chevron" size=${14} />
    </summary>
    <div class="tool-content">
      ${preview
        ? html`<pre tabindex="0" role="region" aria-label="Tool details">
${preview}</pre
          >`
        : html`<p>
            ${tool.status === "running"
              ? "Hermes is using this tool."
              : "This tool has finished."}
          </p>`}
      ${tool.output !== undefined &&
      html`<small>Result</small>
        <pre tabindex="0" role="region" aria-label="Tool result">
${output || "No output"}</pre
        >`}
      ${tool.duration !== undefined && html`<small>${tool.duration}s</small>`}
      ${isAgent &&
      html`<div class="child-facts">
        ${typeof tool.model === "string" &&
        html`<span>${tool.model}</span>`}${duration(tool.duration_seconds) &&
        html`<span>${duration(tool.duration_seconds)}</span>`}${numberValue(
          tool.cost_usd,
        ) !== null &&
        html`<span title="Cost reported by Hermes"
          >${money(tool.cost_usd)} USD</span
        >`}
      </div>`}
      ${isAgent &&
      typeof tool.child_session_id === "string" &&
      html`<button
        class="text-button child-link"
        onClick=${() =>
          openSession(tool.child_session_id, {
            id: state.active,
            title:
              state.sessionDetails?.title ||
              state.sessions.find((s) => s.id === state.active)?.title ||
              "Parent session",
          })}
      >
        Open child session<${Icon} name="link" size=${14} />
      </button>`}
      ${isAgent &&
      tool.status !== "running" &&
      !tool.child_session_id &&
      html`<p class="field-help">
        Hermes did not include a child transcript link.
      </p>`}
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
  images = [],
  record = null,
  canChange = false,
  parts = null,
  responseStatus = null,
  agentName = "Hermes",
}) {
  const [copied, setCopied] = useState(false);
  const toolCards = useMemo(
    () =>
      new Map(
        tools.map((tool) => [
          `${tool.kind === "agent"}:${tool.id}`,
          html`<${ToolCard}
            key=${`${tool.kind === "agent"}:${tool.id}`}
            tool=${tool}
          />`,
        ]),
      ),
    [tools],
  );
  function renderPart(part, index, all) {
    if (part.kind === "reasoning")
      return html`<details class="reasoning" key=${index}>
        <summary>Thinking<${Icon} name="chevron" size=${14} /></summary>
        <${Markdown} text=${part.text} deferHighlight=${!!record} />
      </details>`;
    if (part.kind === "tool")
      return html`<div class="tool-stack" key=${index}>
        ${toolCards.get(`${!!part.agent}:${part.id}`)}
      </div>`;
    if (part.kind === "images")
      return html`<${Images} key=${index} images=${part.images} />`;
    return (
      part.text &&
      html`<div class="message-text" key=${index}>
        <${Markdown}
          text=${part.text}
          streaming=${streaming && index === all.length - 1}
          smooth=${true}
          deferHighlight=${!!record}
        />
      </div>`
    );
  }
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
    aria-label=${role === "user" ? "Your message" : `${agentName} response`}
  >
    ${role !== "user" &&
    html`<div class="message-byline">
      <span class="agent-avatar"><${Icon} name="spark" size=${14} /></span
      ><span>${agentName}</span>${time && html`<time>${humanTime(time)}</time>`}
    </div>`}
    ${role === "user"
      ? html`<${Images} images=${images} />${text &&
          html`<div
            class="user-content message-text"
            tabindex="0"
            role="region"
            aria-label="Your message text"
          >
            ${text}
          </div>`}`
      : (parts || responseParts({ text, reasoning, tools })).map(renderPart)}
    ${streaming &&
    !text &&
    html`<div class="thinking" role="status">
      <span /><span /><span /><span class="sr-only">${agentName} is thinking</span>
    </div>`}
    ${!streaming &&
    (!!text ||
      images.length > 0 ||
      tools.length > 0 ||
      reasoning ||
      parts?.length) &&
    html`<div class="message-actions">
      ${role !== "user" &&
      record?.id &&
      html`<${IconButton}
        name="info"
        label="Response details"
        onClick=${() =>
          update({
            modal: {
              type: "response",
              session: state.active,
              message: {
                ...record,
                responseStatus,
                reasoning_content: reasoning || record.reasoning_content,
              },
            },
          })}
      />`}
      <${IconButton}
        name=${copied ? "check" : "copy"}
        label=${copied ? "Copied" : "Copy message"}
        onClick=${copy}
      />
      ${canChange &&
      html`<${IconButton}
          name=${role === "user" ? "edit" : "refresh"}
          label=${role === "user" ? "Edit and resend" : "Regenerate response"}
          onClick=${() =>
            update({
              modal: {
                type: "message-action",
                action: role === "user" ? "edit" : "regenerate",
                session: state.active,
                message: record,
              },
            })}
        />
        <${IconButton}
          name="trash"
          label="Delete turn"
          onClick=${() =>
            update({
              modal: {
                type: "message-action",
                action: "delete",
                session: state.active,
                message: record,
              },
            })}
        />`}
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
  const unavailable = !supports("run_approval_response");
  return html`<section class="approval-card" aria-label="Approval required">
    <div class="approval-heading">
      <${Icon} name="alert" size=${18} /><strong
        >A moment for your approval</strong
      >
    </div>
    <p>Hermes would like to run this command.</p>
    <pre tabindex="0" role="region" aria-label="Command awaiting approval">
${request.command ||
      request.description ||
      request.preview ||
      "A tool needs your permission to continue."}</pre
    >
    ${error && html`<div class="form-error" role="alert">${error}</div>`}
    <div class="approval-actions">
      ${choices.includes("deny") &&
      html`<button
        class="button secondary"
        disabled=${busy || unavailable}
        onClick=${() => respond("deny")}
      >
        Deny
      </button>`}${choices.includes("session") &&
      html`<button
        class="button secondary"
        disabled=${busy || unavailable}
        onClick=${() => respond("session")}
      >
        Allow for session
      </button>`}${choices.includes("once") &&
      html`<button
        class="button primary"
        disabled=${busy || unavailable}
        onClick=${() => respond("once")}
      >
        ${busy ? "Sending…" : "Allow once"}
      </button>`}
    </div>
    ${unavailable &&
    html`<p>
      This Hermes version cannot receive approvals here. Respond in Hermes to
      continue.
    </p>`}
  </section>`;
}

function historyItems(history, live) {
  const currentImage = currentImageMessage(history, live);
  const items = [];
  const calls = new Map();
  const lastUser = history.findLastIndex((m) => m?.role === "user");
  const currentChildren =
    withoutImagePlaceholders(plainContent(history[lastUser]?.content)) ===
    live?.userText
      ? (live?.tools || []).filter((t) => t.kind === "agent")
      : [];
  for (const [position, message] of history.entries()) {
    if (!message || typeof message !== "object") continue;
    if (message.display_kind === "hidden") continue;
    if (message.role === "tool") {
      const call = calls.get(message.tool_call_id);
      if (call) {
        const output = plainContent(message.content);
        call.output = output.slice(0, 20000);
        call.status = "completed";
        if (call.name === "delegate_task" && output.length <= 250000) {
          try {
            const result = JSON.parse(output);
            const args = JSON.parse(call.preview || "{}");
            const entries = Array.isArray(result)
              ? result
              : result.results ||
                (result.task_index !== undefined ? [result] : []);
            if (Array.isArray(entries))
              call.children = entries
                .slice(0, 32)
                .filter((r) => r && typeof r.summary === "string")
                .map((entry, index) => {
                  const goal =
                    args.tasks?.[entry.task_index]?.goal || args.goal;
                  const matches = call.currentTurn
                    ? currentChildren.filter(
                        (t) =>
                          t.name === goal && t.task_index === entry.task_index,
                      )
                    : [];
                  const prior = matches.length === 1 ? matches[0] : {};
                  return {
                    ...prior,
                    ...entry,
                    kind: "agent",
                    id: `${call.id}-${index}`,
                    name: typeof goal === "string" ? goal : "Delegated task",
                    preview: entry.summary,
                    child_session_id:
                      entry.child_session_id || prior.child_session_id,
                    cost_usd:
                      entry.cost_status === "unknown"
                        ? prior.cost_usd
                        : entry.cost_usd,
                  };
                });
          } catch {
            /* Older tool output remains readable without a structured child card. */
          }
        }
      }
    } else if (message.role === "user" || message.role === "assistant") {
      const tools = (message.tool_calls || []).map((t, i) => ({
        id: t.id || i,
        name: t.function?.name || t.name,
        preview:
          typeof t.function?.arguments === "string"
            ? t.function.arguments
            : JSON.stringify(t.function?.arguments || {}),
        status: "not_reported",
        currentTurn: position > lastUser,
      }));
      for (const tool of tools) calls.set(tool.id, tool);
      items.push({
        record: message,
        role: message.role,
        text:
          message === currentImage
            ? live.userText
            : withoutImagePlaceholders(plainContent(message.content)),
        images: messageImages(
          message === currentImage
            ? { ...message, browserImages: live.userImages }
            : message,
        ),
        isCurrentImage: message === currentImage,
        tools,
        time: message.timestamp || message.created_at,
        reasoning: message.reasoning || message.reasoning_content || "",
        id: message.id || message.row_id || items.length,
      });
    }
  }
  const visible = items.map((m) => ({
    ...m,
    tools: m.tools.flatMap((tool) =>
      tool.children?.length ? tool.children : [tool],
    ),
  }));
  const turns = [];
  // Hermes stores model calls and tool results as separate rows. Present one
  // assistant turn with ordered parts, retaining the final row's native ID for
  // details and rewind actions. Never change the underlying transcript.
  for (const item of visible) {
    if (item.role === "user") {
      turns.push(item);
      continue;
    }
    const parts = [
      ...(item.reasoning ? [{ kind: "reasoning", text: item.reasoning }] : []),
      ...(item.text ? [{ kind: "text", text: item.text }] : []),
      ...(item.images.length ? [{ kind: "images", images: item.images }] : []),
      ...item.tools.map((tool) => ({
        kind: "tool",
        id: tool.id,
        agent: tool.kind === "agent",
      })),
    ];
    const previous = turns.at(-1);
    if (previous?.role === "assistant") {
      previous.parts.push(...parts);
      previous.tools.push(...item.tools);
      previous.text = [previous.text, item.text].filter(Boolean).join("\n\n");
      previous.reasoning = [previous.reasoning, item.reasoning]
        .filter(Boolean)
        .join("\n\n");
      previous.record = item.record;
    } else turns.push({ ...item, tools: [...item.tools], parts });
  }
  return turns.filter((item) => item.role === "user" || item.parts.length);
}

export function Conversation({ app, command, onDismissCommand }) {
  const scroll = useRef();
  const bottom = useRef();
  const sticky = useRef(true);
  const scrollTop = useRef(0);
  const follow = useCallback(() => {
    bottom.current?.scrollIntoView({ behavior: "instant" });
    scrollTop.current = scroll.current?.scrollTop || 0;
  }, []);
  const [away, setAway] = useState(false);
  const [olderBusy, setOlderBusy] = useState(false);
  const live = app.lives[app.active];
  const agentName = app.agent?.name?.trim() || "Hermes";
  const items = useMemo(
    () => historyItems(app.history, live),
    // Snapshot live metadata when saved history/image reconciliation changes, not on text deltas.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [app.history, live?.persisted, live?.userImages],
  );
  const streaming = running(app.active, app.lives);
  useEffect(() => {
    sticky.current = true;
    setAway(false);
    follow();
  }, [app.active, follow]);
  useEffect(() => {
    if (sticky.current) follow();
  }, [app.history, live?.text, live?.tools.length, live?.approval, follow]);
  useEffect(() => {
    // Revealed text can grow between network updates. Follow its actual size,
    // including code highlighting, while respecting a reader scrolling up.
    const content = scroll.current?.querySelector(".conversation-content");
    if (!content) return;
    const observer = new ResizeObserver(() => {
      if (sticky.current) follow();
    });
    observer.observe(content);
    observer.observe(scroll.current);
    return () => observer.disconnect();
  }, [app.active, follow]);
  const lastUser = items.findLast((m) => m.role === "user");
  const savedTurn = live?.persisted && live.savedMessageId != null
    ? items.find((item) => item.record?.id === live.savedMessageId)
    : null;
  const canChange =
    !!app.caps.talaria_extensions?.rewind &&
    (app.sessionDetails || app.sessions.find((s) => s.id === app.active))
      ?.source === "api_server" &&
    !app.readOnlyParent &&
    !running(app.active, app.lives) &&
    !live?.uncertain;
  const showUser =
    live &&
    !live.persisted &&
    !live.userPersisted &&
    (live.userImages?.length
      ? !items.some((message) => message.isCurrentImage)
      : (live.baseUserId !== undefined
          ? lastUser?.record?.id === live.baseUserId
          : app.history.length <= live.baseHistoryLength) ||
        lastUser?.text !== live.userText);
  // History objects stay stable during streaming. Retain their VNodes so each
  // delta does not revisit every old message, tool card, and timestamp.
  const history = useMemo(
    () =>
      !app.loading &&
      items.map(
        (m) =>
          html`<${Message}
            key=${m.id}
            ...${m}
            agentName=${agentName}
            canChange=${canChange && typeof m.record?.id === "number"}
            responseStatus=${m === savedTurn && !live.outcomeUnknown ? live.status : null}
          />`,
      ),
    [items, canChange, app.loading, savedTurn, live?.status, live?.outcomeUnknown, agentName],
  );
  async function loadEarlier() {
    if (olderBusy) return;
    const el = scroll.current,
      previousHeight = el.scrollHeight,
      previousTop = el.scrollTop;
    sticky.current = false;
    setOlderBusy(true);
    try {
      await loadOlderMessages();
      requestAnimationFrame(() => {
        if (el.isConnected)
          el.scrollTop = previousTop + el.scrollHeight - previousHeight;
      });
    } catch (e) {
      fail(e);
    } finally {
      setOlderBusy(false);
    }
  }
  const transcript = command ? [...(history || [])] : history;
  if (command) {
    const next = commandRunning(command) || command.afterId == null ? -1 : items.findIndex(
      (item) => item.role === "user" && Number(item.record?.id) > command.afterId,
    );
    transcript.splice(next < 0 ? transcript.length : next, 0,
      html`<${CommandCard} key=${`command:${command.id}`} activity=${command} onDismiss=${onDismissCommand} />`);
  }
  return html`${app.findOpen &&
    html`<${ConversationFind}
      key=${app.active}
      app=${app}
      root=${scroll}
      onLoadEarlier=${loadEarlier}
      olderBusy=${olderBusy}
    />`}
    <div class="conversation-shell"><div
      class="conversation-viewport"
      ref=${scroll}
      onWheel=${(e) => {
        if (e.deltaY < 0) {
          sticky.current = false;
          setAway(true);
        }
      }}
      onScroll=${(e) => {
        const el = e.currentTarget;
        const top = Math.max(0, el.scrollTop);
        // Small upward movements must release follow even near the bottom.
        // Delayed events after content growth alone must keep following.
        const distance = el.scrollHeight - top - el.clientHeight;
        if (top < scrollTop.current && distance >= 3) sticky.current = false;
        else if (top > scrollTop.current && distance < 3) sticky.current = true;
        scrollTop.current = top;
        setAway(!sticky.current);
      }}
    >
      <div class="conversation-content">
        ${!app.loading &&
        app.historyHasMore &&
        html`<button
          class="load-more"
          disabled=${olderBusy}
          onClick=${loadEarlier}
        >
          ${olderBusy ? "Loading…" : "Load earlier messages"}
        </button>`}
        ${app.loading &&
        html`<div class="history-loading" role="status">
          <span class="skeleton long" /><span class="skeleton" /><span
            class="skeleton medium"
          /><span class="sr-only">Loading session</span>
        </div>`}
        ${transcript}
        ${showUser &&
        html`<${Message}
          role="user"
          text=${live.userText}
          images=${live.userImages}
        />`}
        ${savedTurn === items.at(-1) &&
        savedTurn &&
        !savedTurn.tools.some((t) => t.kind === "agent") &&
        live.tools.some((t) => t.kind === "agent") &&
        html`<div
          class="tool-stack completed-children"
          aria-label="Child agent activity"
        >
          ${live.tools
            .filter((t) => t.kind === "agent")
            .map((tool) => html`<${ToolCard} key=${tool.id} tool=${tool} />`)}
        </div>`}
        ${live &&
        !live.persisted &&
        (streaming || live.text || live.reasoning || live.tools.length) &&
        html`<${Message}
          role="assistant"
          agentName=${agentName}
          text=${live.text}
          tools=${live.tools}
          streaming=${streaming}
          reasoning=${live.reasoning}
          parts=${live.parts}
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
            disabled=${live.retrying}
            onClick=${() => retrySubmission(app.active).catch(fail)}
          >
            ${live.retrying ? "Recovering…" : "Retry submission"}
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
