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
import {
  ActivityGroup,
  AgentCard,
  GuidanceRow,
  Notice,
  ThoughtRow,
  ToolRow,
  activityRuns,
  describeCall,
  terminalExit,
} from "./tool-activity.js";
import { approve, discardRun, retrySubmission, running } from "./runs.js";
import {
  state,
  toast,
  fail,
  supports,
  loadOlderMessages,
  update,
} from "./store.js";
import { guidanceText, isGuidance, isNotice, plainContent, userMessage } from "./content.js";
import {
  currentImageMessage,
  messageImages,
  withoutImagePlaceholders,
} from "./attachments.js";
import { Images } from "./images.js";
import { ConversationFind } from "./conversation-find.js";
import { responseParts } from "./response-parts.js";
import { useReplyJump, useScrollControls, scrollBehavior } from "./reply-jump.js";
import { useHistoryAnchor } from "./history-anchor.js";
import { t, msg, useLanguage } from "./i18n.js";

function Message({
  role,
  text,
  time,
  tools = [],
  streaming = false,
  reasoning = "",
  images = [],
  record = null,
  turnRecord = null,
  canChange = false,
  canBranch = false,
  parts = null,
  responseStatus = null,
  agentName = "Hermes",
  matched = false,
  matchId = null,
  detailsEnabled = true,
  locale,
  formatLocale,
}) {
  const [copied, setCopied] = useState(false);
  // Reconciliation adds a native record to the existing live message. Code
  // already shown must not return to deferred highlighting at that boundary.
  const deferHighlight = useRef(!!record).current;
  // Steps the reader opened. A run that becomes a group mounts its steps
  // again, so they reopen from here instead of collapsing mid-response.
  const openSteps = useRef(new Set()).current;
  const remember = (key) => (open) => (open ? openSteps.add(key) : openSteps.delete(key));
  const toolRows = useMemo(
    () =>
      new Map(
        tools.map((tool) => {
          const key = `tool:${tool.kind === "agent"}:${tool.id}`;
          return [key, { tool, node: tool.kind === "agent"
            ? html`<${AgentCard} key=${key} tool=${tool} />`
            : html`<${ToolRow}
                key=${key}
                tool=${tool}
                matched=${matchId && tool.messageIds?.some((id) => String(id) === String(matchId))}
                initiallyOpen=${openSteps.has(key)}
                onToggle=${remember(key)}
              />` }];
        }),
      ),
    // Tool VNodes contain translated labels and locale-formatted durations/costs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tools, matchId, locale, formatLocale],
  );
  const groups = useRef(new Map());
  const allParts = parts || responseParts({ text, reasoning, tools });
  const partCounts = {};
  // Native history can add or omit reasoning. Keep prose and tool identities
  // independent of the number of preceding parts of another kind.
  const entries = allParts.map((part, index) => {
    const position = partCounts[part.kind] || 0;
    partCounts[part.kind] = position + 1;
    const key = part.kind === "tool"
      ? `tool:${!!part.agent}:${part.id}` : `${part.kind}:${position}`;
    return { kind: part.kind, agent: part.agent, part, key,
      tail: streaming && index === allParts.length - 1 };
  });
  function step(entry) {
    if (entry.kind === "reasoning")
      return html`<${ThoughtRow} key=${entry.key} text=${entry.part.text}
        streaming=${entry.tail} deferHighlight=${deferHighlight}
        initiallyOpen=${openSteps.has(entry.key)} onToggle=${remember(entry.key)} />`;
    return toolRows.get(entry.key)?.node;
  }
  const nextGroups = new Map();
  function group(run) {
    // A group keeps its first tool's identity as later steps arrive.
    const key = run.steps.find((entry) => entry.kind === "tool").key;
    const working = run.steps.some((entry) =>
      entry.tail || toolRows.get(entry.key)?.tool.status === "running");
    // Text deltas re-render the reply. Reuse unchanged groups so Preact skips
    // their steps instead of revisiting every tool.
    const signature = run.steps.flatMap((entry) =>
      [entry.key, entry.kind === "reasoning" ? entry.part.text : "", entry.tail]);
    const previous = groups.current.get(key);
    const groupKey = `group:${key}`;
    // A new group opens if the reader had opened one of the steps it absorbs.
    if (!previous && run.steps.some((entry) => openSteps.has(entry.key))) openSteps.add(groupKey);
    const node = previous?.rows === toolRows && previous.working === working &&
      previous.signature.length === signature.length &&
      previous.signature.every((value, index) => value === signature[index])
      ? previous.node
      : html`<div class="tool-stack" key=${key}>
          <${ActivityGroup} working=${working} initiallyOpen=${openSteps.has(groupKey)}
            onToggle=${remember(groupKey)} steps=${run.steps.map((entry) => ({
            key: entry.key, tool: toolRows.get(entry.key)?.tool, node: step(entry),
          }))} />
        </div>`;
    nextGroups.set(key, { rows: toolRows, working, signature, node });
    return node;
  }
  function renderPart(entry) {
    if (entry.steps) return group(entry);
    if (entry.kind === "reasoning") return step(entry);
    if (entry.kind === "tool")
      return html`<div class="tool-stack" key=${entry.key}>${step(entry)}</div>`;
    if (entry.kind === "images")
      return html`<${Images} key=${entry.key} images=${entry.part.images} />`;
    if (entry.kind === "guidance")
      return html`<${GuidanceRow} key=${entry.key} text=${entry.part.text}
        matched=${matchId && String(entry.part.messageId) === String(matchId)} />`;
    return (
      entry.part.text &&
      html`<div class="message-text" key=${entry.key}
        data-search-target=${matchId && String(entry.part.messageId) === String(matchId) ? "true" : undefined}>
        <${Markdown}
          text=${entry.part.text}
          streaming=${entry.tail}
          smooth=${true}
          deferHighlight=${deferHighlight}
        />
      </div>`
    );
  }
  const content = role === "user" ? null : activityRuns(entries).map(renderPart);
  groups.current = nextGroups;
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      toast(msg("Select the message text to copy it."));
    }
  }
  const showActions = !streaming &&
    !!(text || images.length || tools.length || reasoning || parts?.length);
  return html`<article
    class=${`message ${role} ${matched ? "search-match" : ""}`}
    tabindex=${role === "assistant" ? -1 : undefined}
    aria-label=${role === "user" ? t("Your message") : t("{name} response", { name: agentName })}
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
            aria-label=${t("Your message text")}
          >
            ${text}
          </div>`}`
      : content}
    ${streaming &&
    !text &&
    html`<div class="thinking" role="status">
      <span /><span /><span /><span class="sr-only">${t("{name} is thinking", { name: agentName })}</span>
    </div>`}
    ${(role !== "user" || showActions) &&
    html`<div class="message-actions">
      ${showActions && html`${role !== "user" &&
      detailsEnabled && record?.id &&
      html`<${IconButton}
        name="info"
        label=${t("Response details")}
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
        label=${copied ? t("Copied") : t("Copy message")}
        onClick=${copy}
      />
      ${canBranch && html`<${IconButton}
        name="branch"
        label=${t("Branch from here")}
        onClick=${() => update({ modal: {
          type: "fork",
          session: { ...(state.sessionDetails || state.sessions.find((s) => s.id === state.active)), id: state.active },
          message: record,
        } })}
      />`}
      ${canChange &&
      html`<${IconButton}
          name=${role === "user" ? "edit" : "refresh"}
          label=${role === "user" ? t("Edit and resend") : t("Regenerate response")}
          onClick=${() =>
            update({
              modal: {
                type: "message-action",
                action: role === "user" ? "edit" : "regenerate",
                session: state.active,
                message: turnRecord || record,
              },
            })}
        />
        <${IconButton}
          name="trash"
          label=${t("Delete turn")}
          onClick=${() =>
            update({
              modal: {
                type: "message-action",
                action: "delete",
                session: state.active,
                message: turnRecord || record,
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
  return html`<section class="approval-card" aria-label=${t("Approval required")}>
    <div class="approval-heading">
      <${Icon} name="alert" size=${18} /><strong
        >${t("A moment for your approval")}</strong
      >
    </div>
    <p>${t("Hermes would like to run this command.")}</p>
    <pre tabindex="0" role="region" aria-label=${t("Command awaiting approval")}>
${request.command ||
      request.description ||
      request.preview ||
      t("A tool needs your permission to continue.")}</pre
    >
    ${error && html`<div class="form-error" role="alert">${t(error)}</div>`}
    <div class="approval-actions">
      ${choices.includes("deny") &&
      html`<button
        class="button secondary"
        disabled=${busy || unavailable}
        onClick=${() => respond("deny")}
      >
        ${t("Deny")}
      </button>`}${choices.includes("session") &&
      html`<button
        class="button secondary"
        disabled=${busy || unavailable}
        onClick=${() => respond("session")}
      >
        ${t("Allow for this response")}
      </button>`}${choices.includes("once") &&
      html`<button
        class="button primary"
        disabled=${busy || unavailable}
        onClick=${() => respond("once")}
      >
        ${busy ? t("Sending…") : t("Allow once")}
      </button>`}
    </div>
    ${unavailable &&
    html`<p>
      ${t("This Hermes version cannot receive approvals here. Respond in Hermes to continue.")}
    </p>`}
  </section>`;
}

function orphanTool(message) {
  const tool = { id: message.tool_call_id || message.id, name: message.tool_name || "",
    status: "finished", messageIds: [message.id] };
  return { record: message, id: message.id, role: "assistant", text: "", images: [],
    tools: [tool], time: message.timestamp, reasoning: "" };
}

// Hermes saves a turn's steps while it runs, so a page that reconnects to the
// run also loads some of them as history. Keep the saved prompt and show the
// rest of that turn once, from the live copy.
function withoutLiveTurn(history, live) {
  if (!live || live.persisted || typeof live.baseMessageId !== "number") return history;
  const prompt = history.findLastIndex((message) => userMessage(message) &&
    typeof message.id === "number" && message.id > live.baseMessageId &&
    withoutImagePlaceholders(plainContent(message.content)) === live.userText);
  return prompt < 0 || prompt === history.length - 1 ? history : history.slice(0, prompt + 1);
}

function historyItems(history, live, excerpt = false) {
  const currentImage = currentImageMessage(history, live);
  const items = [];
  const calls = new Map();
  const lastUser = history.findLastIndex(userMessage);
  const currentChildren =
    withoutImagePlaceholders(plainContent(history[lastUser]?.content)) ===
    live?.userText
      ? (live?.tools || []).filter((t) => t.kind === "agent")
      : [];
  // Only enrich the exact settled native turn. Repeated prompts or reused call
  // IDs in older turns must not inherit the most recent run's outcome.
  const settledEnd = live?.persisted && live.savedMessageId != null
    ? history.findIndex((m) => m?.id === live.savedMessageId && m.role === "assistant") : -1;
  const settledStart = history.findLastIndex((m, i) => i < settledEnd && userMessage(m));
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
      if (call?.output !== undefined) {
        // In-place compaction appends a pruned copy of an old result under the
        // same call ID. The result displayed in place is the original.
        call.messageIds.push(message.id);
      } else if (call) {
        const output = plainContent(message.content);
        call.output = output;
        call.messageIds.push(message.id);
        const known = position > settledStart && position <= settledEnd ? outcomes.get(call.id) : null;
        if (call.name === "terminal") call.exitCode = terminalExit(output);
        call.status = known?.status ||
          (call.exitCode != null ? (call.exitCode ? "failed" : "completed") : "finished");
        if (known) Object.assign(call, { duration: known.duration, startedAt: known.startedAt,
          endedAt: known.endedAt });
        if (call.name === "delegate_task" && output.length <= 250000) {
          try {
            const result = JSON.parse(output);
            const args = JSON.parse(call.args || "{}");
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
                    name: typeof goal === "string" ? goal : "",
                    summary: entry.summary,
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
    } else if (isNotice(message)) {
      items.push({ record: message, role: "notice", kind: message.display_kind,
        text: plainContent(message.content), images: [], tools: [], reasoning: "",
        time: message.timestamp, id: message.id || items.length });
    } else if (isGuidance(message)) {
      items.push({ record: message, role: "assistant", guidance: guidanceText(message.content),
        text: "", images: [], tools: [], reasoning: "", time: message.timestamp,
        id: message.id || items.length });
    } else if (message.role === "user" || message.role === "assistant") {
      const tools = (message.tool_calls || []).map((t, i) => {
        const name = t.function?.name || t.name;
        const args = typeof t.function?.arguments === "string"
          ? t.function.arguments
          : JSON.stringify(t.function?.arguments || {});
        return {
          id: t.id || i,
          name,
          args,
          ...describeCall(name, args),
          status: "not_reported",
          currentTurn: position > lastUser,
          messageIds: [message.id],
        };
      });
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
    if (item.role !== "assistant") {
      turns.push(item);
      continue;
    }
    const parts = [
      ...(item.guidance ? [{ kind: "guidance", text: item.guidance, messageId: item.record.id }] : []),
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
      // Details and branching use the reply's final row. Hermes counts guidance
      // as a user turn, so regenerate and delete use the turn's first row.
      if (!item.guidance) previous.record = item.record;
    } else turns.push({ ...item, tools: [...item.tools], parts, turnRecord: item.record });
  }
  return turns.filter((item) => item.role !== "assistant" || item.parts.length);
}

// Where a command result belongs: before the first row Hermes saved after the
// command began. In-place compaction re-saves kept rows with new IDs, so
// Hermes's timestamps, which it keeps, order them.
function anchorIndex(items, anchor, end) {
  if (anchor === null) return end;
  const index = items.findIndex((item) => Number(item.time ?? item.record?.timestamp) > anchor);
  return index < 0 ? end : Math.min(index, end);
}

export function Conversation({ app, commands = [], onDismissCommand }) {
  const { locale, formatLocale } = useLanguage();
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
  const controls = useScrollControls(app.active, away || !!replyStart);
  const [olderBusy, setOlderBusy] = useState(false);
  const live = app.searchWindow ? null : app.lives[app.active];
  const agentName = app.agent?.name?.trim() || "Hermes";
  // The same history object unless part of it is showing live.
  const shown = useMemo(
    () => withoutLiveTurn(app.history, live),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [app.history, live?.persisted, live?.baseMessageId, live?.userText],
  );
  const items = useMemo(
    () => historyItems(shown, live, !!app.searchWindow),
    // Snapshot live metadata when saved history/image reconciliation changes, not on text deltas.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shown, live?.persisted, live?.userImages, app.searchWindow],
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
    // A matched step can be folded inside an activity group.
    for (const target of tools)
      for (let details = target.closest("details"); details; details = details.parentElement.closest("details"))
        details.open = true;
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
  const canBranch = canChange && supports("session_fork");
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
        (m) => m.role === "notice"
          ? html`<${Notice} key=${messageKeys.get(m.id)} kind=${m.kind} text=${m.text} time=${m.time}
              matched=${app.searchWindow && String(m.record.id) === String(app.searchWindow)} />`
          : html`<${Message}
            key=${messageKeys.get(m.id)}
            ...${m}
            agentName=${agentName}
            locale=${locale}
            formatLocale=${formatLocale}
            matched=${app.searchWindow && m.messageIds.some((id) => String(id) === String(app.searchWindow))}
            matchId=${app.searchWindow}
            detailsEnabled=${!app.searchWindow}
            canChange=${canChange && typeof m.record?.id === "number" &&
              (m.turnRecord || m.record).rewindable !== false}
            canBranch=${canBranch && m.role === "assistant" && m.record?.branchable === true}
            responseStatus=${m === savedTurn && !live.outcomeUnknown ? live.status : null}
          />`,
      ),
    [items, messageKeys, canChange, canBranch, app.loading, savedTurn, live?.status, live?.outcomeUnknown, agentName, app.searchWindow, locale, formatLocale],
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
  const saved = history || [];
  const anchored = [], trailing = [];
  for (const activity of commands) {
    const key = `command:${activity.id}`;
    const node = activity.compaction
      ? html`<${Notice} key=${key} kind="compaction" text=${activity.text} time=${activity.finished} />`
      : html`<${CommandCard} key=${key} activity=${activity} onDismiss=${() => onDismissCommand(activity)} />`;
    // A settled reply can still live outside history while reconciliation is
    // pending. A command begun after it stays below it.
    const afterLive = live && !live.persisted && Number.isFinite(live.createdAt) &&
      activity.started >= live.createdAt;
    if (commandRunning(activity) || afterLive) {
      trailing.push(node);
      continue;
    }
    const index = anchorIndex(items, activity.anchor, saved.length);
    // Before the first loaded row, it belongs to history not loaded yet.
    if (index > 0 || !app.historyHasMore) anchored.push([index, node]);
  }
  anchored.sort((a, b) => a[0] - b[0]);
  const transcript = [];
  for (let index = 0, next = 0; index <= saved.length; index++) {
    while (next < anchored.length && anchored[next][0] === index) transcript.push(anchored[next++][1]);
    if (index < saved.length) transcript.push(saved[index]);
  }
  if (showUser)
    transcript.push(html`<${Message}
      key=${`${liveKey}:user`}
      role="user"
      locale=${locale}
      formatLocale=${formatLocale}
      text=${live.userText}
      images=${live.userImages}
    />`);
  if (live && !live.persisted &&
      (streaming || live.text || live.reasoning || live.tools.length))
    transcript.push(html`<${Message}
      key=${`${liveKey}:assistant`}
      role="assistant"
      locale=${locale}
      formatLocale=${formatLocale}
      agentName=${agentName}
      text=${live.text}
      tools=${live.tools}
      streaming=${streaming && !live.clarification}
      reasoning=${live.reasoning}
      parts=${live.parts}
    />`);
  transcript.push(...trailing);
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
        controls.reveal();
        if (e.deltaY < 0) {
          sticky.current = false;
          setAway(true);
        }
      }}
      onPointerDown=${controls.reveal}
      onKeyDown=${(e) => {
        if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(e.key))
          controls.reveal();
      }}
      onScroll=${(e) => {
        const el = e.currentTarget;
        const top = Math.max(0, el.scrollTop);
        // follow() records its own position: automatic streaming scrolls must
        // not keep the shortcuts visible after the reader stops interacting.
        if (top !== scrollTop.current) controls.reveal();
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
          ${olderBusy ? t("Loading…") : t("Load earlier messages")}
        </button>`}
        ${app.loading &&
        html`<div class="history-loading" role="status">
          <span class="skeleton long" /><span class="skeleton" /><span
            class="skeleton medium"
          /><span class="sr-only">${t("Loading session")}</span>
        </div>`}
        ${transcript}
        ${savedTurn === items.at(-1) &&
        savedTurn &&
        !savedTurn.tools.some((t) => t.kind === "agent") &&
        live.tools.some((t) => t.kind === "agent") &&
        html`<div
          class="tool-stack completed-children"
          aria-label=${t("Child agent activity")}
        >
          ${live.tools
            .filter((t) => t.kind === "agent")
            .map((tool) => html`<${AgentCard} key=${tool.id} tool=${tool} />`)}
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
          <span class="spinner" /> ${t("Reconnecting to Hermes. The response status is not yet confirmed.")}
        </div>`}
        ${live?.error &&
        html`<div class="run-error" role="alert">
          ${t(live.error)}${live.submissionFailed &&
          html`<button
            class="text-button"
            disabled=${live.retrying}
            onClick=${() => retrySubmission(app.active).catch(fail)}
          >
            ${live.retrying ? t("Recovering…") : t("Retry submission")}
          </button>`}
        </div>`}
        ${live?.status === "unknown" &&
        html`<div class="run-error" role="alert">
          ${t("Hermes restarted before this response finished. Some actions may already have run, so check them before sending this message again.")}
          <button
            class="text-button"
            title=${t("Hermes will not run it again, and this conversation can continue.")}
            disabled=${live.discarding}
            onClick=${() => discardRun(app.active).catch(fail)}
          >
            ${live.discarding ? t("Discarding…") : t("Discard and continue")}
          </button>
        </div>`}
        ${live?.turnLimitReached &&
        html`<div class="run-notice" role="status">
          ${t("Hermes reached its turn limit. Send another message to continue.")}
        </div>`}
        ${live?.status === "cancelled" && !live.discarded &&
        html`<div class="run-notice">${t("You stopped this response.")}</div>`}
        ${live?.pendingSteer &&
        html`<div class="run-notice">
          ${t("Hermes did not receive this guidance before the response finished: {guidance}", { guidance: live.pendingSteer })}
        </div>`}
        <div ref=${bottom} class="scroll-anchor" />
      </div>
    </div>
      ${(away || replyStart) && html`<div class=${`conversation-jumps${controls.visible ? "" : " is-idle"}`}>
      ${replyStart && html`<button
        class="jump-reply"
        aria-label=${t("Jump to start of reply")}
        title=${t("Jump to start of reply")}
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
      ><${Icon} name="arrow" size=${16} /></button>`}
      ${away &&
      html`<button
        class="jump-bottom"
        aria-label=${t("Jump to latest message")}
        title=${t("Jump to latest message")}
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
