import {
  html,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useMemo,
  useCallback,
  Icon,
  IconButton,
  humanTime,
} from "./lib.js";
import { CommandCard, commandRunning } from "./command-activity.js";
import { Clarification } from "./clarification.js";
import { Markdown } from "./markdown.js";
import { ToolCode } from "./tool-code.js";
import { ToolResult } from "./tool-result.js";
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
import { useReplyJump, scrollBehavior } from "./reply-jump.js";
import { useHistoryAnchor } from "./history-anchor.js";

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

function ToolCard({ tool, matched = false }) {
  const [open, setOpen] = useState(false);
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
  const icon = isAgent
    ? "branch"
    : /search|browse|web/.test(tool.name)
      ? "globe"
      : /file|read|write/.test(tool.name)
        ? "file"
        : "terminal";
  return html`<details
    class=${`tool-card ${tool.status === "running" ? "working" : ""} ${matched ? "search-tool-match" : ""}`}
    data-search-target=${matched ? "true" : undefined}
    onToggle=${(event) => setOpen(event.currentTarget.open)}
  >
    <summary>
      <span class="tool-icon"><${Icon} name=${icon} size=${17} /></span
      ><span class="tool-label"
        >${isAgent
          ? "Subagent"
          : (tool.name || "Tool").replace(/_/g, " ")}${isAgent &&
          html`<small>${tool.name}</small>`}</span
      ><span class="tool-status" title=${tool.status === "finished" ? "Outcome not reported by Hermes" : undefined}
        >${tool.status === "running"
          ? html`<span class="spinner" />`
          : html`<${Icon}
              name=${failed ? "alert" : ["not_reported", "finished"].includes(tool.status) ? "info" : "check"}
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
        ? html`<${ToolCode} text=${preview} label="Tool details"
            shell=${!isAgent && tool.name === "terminal"} enabled=${open} />`
        : html`<p>
            ${tool.status === "running"
              ? "Hermes is using this tool."
              : "This tool has finished."}
          </p>`}
      ${tool.output !== undefined &&
      html`<small>Result</small>
        <${ToolResult} text=${tool.output}
          enabled=${open && tool.status !== "running"} />`}
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
  matched = false,
  matchId = null,
  detailsEnabled = true,
}) {
  const [copied, setCopied] = useState(false);
  // Reconciliation adds a native record to the existing live message. Code
  // already shown must not return to deferred highlighting at that boundary.
  const deferHighlight = useRef(!!record).current;
  const toolCards = useMemo(
    () =>
      new Map(
        tools.map((tool) => [
          `${tool.kind === "agent"}:${tool.id}`,
          html`<${ToolCard}
            key=${`${tool.kind === "agent"}:${tool.id}`}
            tool=${tool}
            matched=${matchId && tool.messageIds?.some((id) => String(id) === String(matchId))}
          />`,
        ]),
      ),
    [tools, matchId],
  );
  const partCounts = {};
  function renderPart(part, index, all) {
    const position = partCounts[part.kind] || 0;
    partCounts[part.kind] = position + 1;
    // Native history can add or omit reasoning. Keep prose and tool identities
    // independent of the number of preceding parts of another kind.
    const key = part.kind === "tool"
      ? `tool:${!!part.agent}:${part.id}` : `${part.kind}:${position}`;
    if (part.kind === "reasoning")
      return html`<details class="reasoning" key=${key}>
        <summary>Thinking<${Icon} name="chevron" size=${14} /></summary>
        <${Markdown} text=${part.text} streaming=${streaming && index === all.length - 1}
          deferHighlight=${deferHighlight} />
      </details>`;
    if (part.kind === "tool")
      return html`<div class="tool-stack" key=${key}>
        ${toolCards.get(`${!!part.agent}:${part.id}`)}
      </div>`;
    if (part.kind === "images")
      return html`<${Images} key=${key} images=${part.images} />`;
    return (
      part.text &&
      html`<div class="message-text" key=${key}
        data-search-target=${matchId && String(part.messageId) === String(matchId) ? "true" : undefined}>
        <${Markdown}
          text=${part.text}
          streaming=${streaming && index === all.length - 1}
          smooth=${true}
          deferHighlight=${deferHighlight}
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
  const showActions = !streaming &&
    !!(text || images.length || tools.length || reasoning || parts?.length);
  return html`<article
    class=${`message ${role} ${matched ? "search-match" : ""}`}
    tabindex=${role === "assistant" ? -1 : undefined}
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
    ${(role !== "user" || showActions) &&
    html`<div class="message-actions">
      ${showActions && html`${role !== "user" &&
      detailsEnabled && record?.id &&
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
        />`}`}
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

function orphanTool(message) {
  const tool = { id: message.tool_call_id || message.id, name: message.tool_name || "Tool",
    status: "finished", messageIds: [message.id] };
  return { record: message, id: message.id, role: "assistant", text: "", images: [],
    tools: [tool], time: message.timestamp, reasoning: "" };
}

function historyItems(history, live, excerpt = false) {
  const currentImage = currentImageMessage(history, live);
  const items = [];
  const calls = new Map();
  const lastUser = history.findLastIndex((m) => m?.role === "user");
  const currentChildren =
    withoutImagePlaceholders(plainContent(history[lastUser]?.content)) ===
    live?.userText
      ? (live?.tools || []).filter((t) => t.kind === "agent")
      : [];
  // Only enrich the exact settled native turn. Repeated prompts or reused call
  // IDs in older turns must not inherit the most recent run's outcome.
  const settledEnd = live?.persisted && live.savedMessageId != null
    ? history.findIndex((m) => m?.id === live.savedMessageId && m.role === "assistant") : -1;
  const settledStart = history.findLastIndex((m, i) => i < settledEnd && m?.role === "user");
  const outcomes = new Map(settledStart >= 0 ? (live.tools || [])
    .filter((t) => t.kind !== "agent" && t.id != null &&
      !["running", "not_reported"].includes(t.status))
    .map((t) => [t.id, t]) : []);
  for (const [position, message] of history.entries()) {
    if (!message || typeof message !== "object") continue;
    if (message.display_kind === "hidden") continue;
    if (message.role === "tool") {
      let call = calls.get(message.tool_call_id);
      if (!call && excerpt) {
        const item = orphanTool(message);
        items.push(item);
        call = item.tools[0];
      }
      if (call) {
        const output = plainContent(message.content);
        call.output = output;
        call.messageIds.push(message.id);
        const known = position > settledStart && position <= settledEnd ? outcomes.get(call.id) : null;
        call.status = known?.status || "finished";
        if (known?.duration !== undefined) call.duration = known.duration;
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
        messageIds: [message.id],
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
    messageIds: [m.record.id, ...m.tools.flatMap((tool) => tool.messageIds)],
    tools: m.tools.flatMap((tool) =>
      !excerpt && tool.children?.length ? tool.children : [tool],
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
      ...(item.text ? [{ kind: "text", text: item.text, messageId: item.record.id }] : []),
      ...(item.images.length ? [{ kind: "images", images: item.images }] : []),
      ...item.tools.map((tool) => ({
        kind: "tool",
        id: tool.id,
        agent: tool.kind === "agent",
      })),
    ];
    const previous = turns.at(-1);
    if (previous?.role === "assistant") {
      previous.messageIds.push(...item.messageIds);
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
  const replyStart = useReplyJump(scroll, app.active);
  const bottom = useRef();
  const sticky = useRef(true);
  useHistoryAnchor(scroll, app.active, app.history, sticky);
  const scrollTop = useRef(0);
  const follow = useCallback(() => {
    bottom.current?.scrollIntoView({ behavior: "instant" });
    scrollTop.current = scroll.current?.scrollTop || 0;
  }, []);
  const [away, setAway] = useState(false);
  const [olderBusy, setOlderBusy] = useState(false);
  const live = app.searchWindow ? null : app.lives[app.active];
  const agentName = app.agent?.name?.trim() || "Hermes";
  const items = useMemo(
    () => historyItems(app.history, live, !!app.searchWindow),
    // Snapshot live metadata when saved history/image reconciliation changes, not on text deltas.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [app.history, live?.persisted, live?.userImages, app.searchWindow],
  );
  const streaming = !app.searchWindow && running(app.active, app.lives);
  useEffect(() => {
    sticky.current = !app.searchWindow;
    setAway(false);
    if (sticky.current) follow();
  }, [app.active, app.searchWindow, follow]);
  useLayoutEffect(() => {
    if (!app.searchWindow || app.loading) return;
    const tools = scroll.current?.querySelectorAll("[data-search-target]") || [];
    for (const target of tools) if (target.tagName === "DETAILS") target.open = true;
    const target = tools[0] || scroll.current?.querySelector(".search-match");
    target?.scrollIntoView({ block: "center", behavior: "instant" });
  }, [app.searchWindow, app.loading, app.history]);
  useEffect(() => {
    if (sticky.current) follow();
  }, [app.history, live?.text, live?.tools.length, live?.approval, live?.clarification, follow]);
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
    !app.readOnlyParent && !app.searchWindow &&
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
  const liveKey = `run:${live?.requestId || live?.id}`;
  const savedUser = savedTurn
    ? items[items.indexOf(savedTurn) - 1]
    : null;
  const previousKeys = useRef(new Map());
  const messageKeys = useMemo(() => {
    // The optimistic messages and their native rows share one keyed list.
    // Retain adopted keys when the next run replaces the live cache, and keep
    // this small identity map bounded to the currently loaded history.
    const keys = new Map(items.map((item) => [item.id,
      previousKeys.current.get(item.id) ||
        (item === savedTurn ? `${liveKey}:assistant`
          : item === savedUser ? `${liveKey}:user` : `history:${item.id}`),
    ]));
    previousKeys.current = keys;
    return keys;
  }, [items, savedTurn, savedUser, liveKey]);
  // History objects stay stable during streaming. Retain their VNodes so each
  // delta does not revisit every old message, tool card, and timestamp.
  const history = useMemo(
    () =>
      !app.loading &&
      items.map(
        (m) =>
          html`<${Message}
            key=${messageKeys.get(m.id)}
            ...${m}
            agentName=${agentName}
            matched=${app.searchWindow && m.messageIds.some((id) => String(id) === String(app.searchWindow))}
            matchId=${app.searchWindow}
            detailsEnabled=${!app.searchWindow}
            canChange=${canChange && typeof m.record?.id === "number"}
            responseStatus=${m === savedTurn && !live.outcomeUnknown ? live.status : null}
          />`,
      ),
    [items, messageKeys, canChange, app.loading, savedTurn, live?.status, live?.outcomeUnknown, agentName, app.searchWindow],
  );
  async function loadEarlier() {
    if (olderBusy) return;
    sticky.current = false;
    setOlderBusy(true);
    try {
      await loadOlderMessages();
    } catch (e) {
      fail(e);
    } finally {
      setOlderBusy(false);
    }
  }
  const transcript = [...(history || [])];
  if (command) {
    const next = commandRunning(command) || command.afterId == null ? -1 : items.findIndex(
      (item) => item.role === "user" && Number(item.record?.id) > command.afterId,
    );
    transcript.splice(next < 0 ? transcript.length : next, 0,
      html`<${CommandCard} key=${`command:${command.id}`} activity=${command} onDismiss=${onDismissCommand} />`);
  }
  if (showUser)
    transcript.push(html`<${Message}
      key=${`${liveKey}:user`}
      role="user"
      text=${live.userText}
      images=${live.userImages}
    />`);
  if (live && !live.persisted &&
      (streaming || live.text || live.reasoning || live.tools.length))
    transcript.push(html`<${Message}
      key=${`${liveKey}:assistant`}
      role="assistant"
      agentName=${agentName}
      text=${live.text}
      tools=${live.tools}
      streaming=${streaming && !live.clarification}
      reasoning=${live.reasoning}
      parts=${live.parts}
    />`);
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
        ${live?.approval &&
        html`<${Approval}
          key=${live.approval.request_id || live.id}
          sid=${app.active}
          request=${live.approval}
        />`}
        ${live?.clarification && html`<${Clarification}
          key=${live.clarification.request_id} sid=${app.active} request=${live.clarification} />`}
        ${live?.statusText && !live.clarification && html`<div class="run-notice" role="status">
          ${live.statusText}</div>`}
        ${live?.reconnecting &&
        html`<div class="run-notice" role="status">
          <span class="spinner" /> Reconnecting to Hermes. The response status is not yet confirmed.
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
      ${(away || replyStart) && html`<div class="conversation-jumps">
      ${replyStart && html`<button
        class="jump-reply"
        aria-label="Jump to start of reply"
        onClick=${() => {
          if (!replyStart.isConnected) return;
          sticky.current = false;
          setAway(true);
          replyStart.focus({ preventScroll: true });
          const viewport = scroll.current;
          viewport.scrollTo({
            // Layout coordinates exclude the reply's entrance animation.
            top: replyStart.offsetTop - 16,
            behavior: scrollBehavior(),
          });
        }}
      ><${Icon} name="arrow" size=${16} />Reply start</button>`}
      ${away &&
      html`<button
        class="jump-bottom"
        aria-label="Jump to latest message"
        onClick=${() => {
          sticky.current = true;
          bottom.current?.scrollIntoView({ behavior: scrollBehavior() });
        }}
      >
        <${Icon} name="down" size=${18} />
      </button>`}
      </div>`}
    </div>`;
}
