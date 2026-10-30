import { api, RequestError, errorMessage, eventSource, streamsUseConnections } from "./api.js";
import { rememberSession } from "./session-navigation.js";
import {
  state,
  update,
  fail,
  toast,
  refreshHistory,
  refreshSessions,
  navigationVersion,
  chooseModel,
  chooseReasoning,
  refreshSessionDetails,
  refreshReadiness,
  restoreDraft,
} from "./store.js";
import { randomId, readStorage, writeStorage } from "./lib.js";
import {
  pendingStorage,
  consumePendingImages,
  cacheMessageImages,
  imageFingerprint,
  currentImageMessage,
  placeholderCount,
  withoutImagePlaceholders,
} from "./attachments.js";
import { plainContent, userMessage } from "./content.js";
import { responseParts, appendText, finishText, interimText } from "./response-parts.js";
import { msg } from "./i18n.js";

const sources = new Map();
// Runs waiting off screen, and where each run's stream stopped.
const parkedRuns = new Set();
const cursors = new Map();
let parkedCheck = null;
let recoveryRecords = {};
// Saved runs this tab finished recovering, so it may remove their records.
const recoveredRuns = new Set();
let restoration, recovered = false;
// Hermes holds a turn lost in a restart as "unknown" until someone discards it.
// Nothing more arrives for it, so the browser settles it as uncertain.
const terminal = new Set(["completed", "failed", "cancelled", "interrupted", "unknown"]);
const receiptKey = (sid, requestId) => `run.${sid}.${requestId}`;
function retainedSize(live) {
  // Conservative UTF-16 payload estimate; shared strings may be counted twice.
  // Only inspect settled candidates, never the active stream on each delta.
  const values = [live.text, live.reasoning, live.userText, live.pendingSteer,
    ...(live.parts || []).map((part) => part.text),
    ...(live.tools || []).flatMap((tool) => [tool.subject, tool.summary, tool.output])];
  return values.reduce((size, value) => size + (typeof value === "string" ? value.length * 2 : 0), 0);
}
function publish(sid, live) {
  const lives = { ...state.lives, [sid]: live };
  if (live.persisted && terminal.has(live.status)) {
    // Hermes already holds these transcripts. Keep a small presentation cache
    // instead of retaining every streamed reply for the lifetime of the tab.
    // Never evict active, uncertain, or unsaved-image recovery state.
    const disposable = Object.entries(lives).filter(
      ([id, item]) =>
        id !== state.active &&
        item.persisted &&
        terminal.has(item.status) &&
        !item.uncertain &&
        !item.imageReceipt,
    );
    let budget = 8 * 1024 * 1024, count = 0;
    for (const [id, item] of disposable.reverse()) {
      const size = retainedSize(item);
      if (++count > 16 || size > budget) delete lives[id];
      else budget -= size;
    }
  }
  update({ lives }, true);
}
function remember() {
  const entries = Object.entries(state.lives)
    .filter(([, r]) => !terminal.has(r.status) || r.uncertain || r.imageReceipt)
    .slice(-16)
    .map(([sid, r]) => [
      sid,
      {
        id: r.id,
        requestId: r.requestId,
        userText: r.userText,
        baseUserId: r.baseUserId,
        baseMessageId: r.baseMessageId,
        imageReceipt: !!r.imageReceipt,
        // When the turn began orders it against command results.
        createdAt: r.createdAt,
        ...(!r.id
          ? r.payload?.images?.length
            ? { imageReceipt: true }
            : { payload: r.payload }
          : {}),
      },
    ]);
  // Every tab of this browser shares these records. Keep another tab's record
  // until a tab has seen that run through.
  const stored = savedRuns();
  const settled = new Set([...Object.values(state.lives).map(runKey), ...recoveredRuns]);
  for (const [sid, record] of Object.entries(stored))
    if (settled.has(runKey(record))) delete stored[sid];
  const saved = { ...stored, ...recoveryRecords, ...Object.fromEntries(entries) };
  writeStorage(
    "runs",
    JSON.stringify(Object.fromEntries(Object.entries(saved).slice(-16))),
  );
}
const runKey = (record) => record?.id || record?.requestId || null;
function savedRuns() {
  try {
    const saved = JSON.parse(readStorage("runs", "{}"));
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(Object.entries(saved).filter(([, record]) =>
      record && typeof record === "object" && !Array.isArray(record)));
  } catch {
    return {};
  }
}
function canRetry(live) {
  if (!live.uncertain) return true;
  const protection = state.caps.features?.runs_idempotency;
  const retention = Math.min(protection?.retention_seconds || 0, 86400) * 1000;
  return (
    protection?.supported === true &&
    retention > 0 &&
    Number.isFinite(live.createdAt) &&
    Date.now() >= live.createdAt &&
    Date.now() - live.createdAt < retention
  );
}
const receiptError =
  msg("The submission confirmation was lost. Check the session before sending another message.");
function turnLimitReached(result) {
  return result.completed === false && !result.error && !result.interrupted && !result.partial &&
    typeof result.turn_exit_reason === "string" &&
    result.turn_exit_reason.startsWith("max_iterations_reached(") &&
    typeof result.output === "string" && !!result.output.trim();
}
// Session-authority prompts are answered with their prompt ID and execution generation.
function nativePrompt(prompt) {
  return typeof prompt?.prompt_id === "string" && prompt.request_id === undefined
    ? { ...prompt, request_id: prompt.prompt_id }
    : prompt;
}
function pendingControl(status, kind) {
  const controls = Array.isArray(status.pending_controls) ? status.pending_controls : [];
  const prompt = controls.find((item) => item?.kind === kind && typeof item.prompt_id === "string");
  return prompt ? nativePrompt(prompt) : null;
}
const generation = (prompt) =>
  Number.isInteger(prompt?.execution_generation)
    ? { execution_generation: prompt.execution_generation } : {};
function runError(status, result) {
  return result.error || (status === "failed" && !turnLimitReached(result))
    ? errorMessage(result.error, msg("Hermes could not finish this response. Check the model/provider and try again."))
    : null;
}
export const running = (sid, lives = state.lives) =>
  Object.hasOwn(lives, sid) && !!lives[sid] && !terminal.has(lives[sid].status);

function runID(result) {
  if (typeof result?.run_id === "string" && result.run_id) return result.run_id;
  throw new RequestError(
    msg("The submission confirmation was incomplete."),
    0,
    "invalid_response",
  );
}

export function clearCompletedRun(sid) {
  if (running(sid) || state.lives[sid]?.uncertain)
    throw new Error(
      msg("Wait for the current response to finish before changing this turn."),
    );
  sources.get(sid)?.close();
  sources.delete(sid);
  const lives = { ...state.lives };
  delete lives[sid];
  update({ lives });
  remember();
}

export function applyEvent(live, event) {
  if (!event || typeof event !== "object" || Array.isArray(event)) return live;
  const type = event.event || event.type;
  if (typeof type !== "string") return live;
  const next = { ...live };
  if (type === "talaria.reasoning.delta" && typeof event.delta === "string") {
    const parts = responseParts(live);
    const tail = parts.at(-1);
    next.parts = tail?.kind === "reasoning" && tail.id === event.block_id
      ? [...parts.slice(0, -1), { ...tail, text: tail.text + event.delta }]
      : [...parts, { kind: "reasoning", id: event.block_id, text: event.delta }];
    next.reasoning = (live.reasoning || "") + event.delta;
    next.statusText = "";
  }
  if (type === "talaria.status" && typeof event.text === "string")
    next.statusText = event.text;
  if (type === "talaria.clarification.request" && typeof event.request_id === "string") {
    next.clarification = event;
    next.status = "waiting_for_input";
    next.statusText = "";
  }
  if (type === "talaria.clarification.resolved" && live.clarification?.request_id === event.request_id) {
    next.clarification = null;
    next.status = "running";
  }
  // Hermes 0.22.1+ asks through its session authority, keyed by prompt and generation.
  if (type === "clarify.request" && typeof event.prompt_id === "string") {
    next.clarification = nativePrompt(event);
    next.status = "waiting_for_input";
    next.statusText = "";
  }
  if (type === "clarify.settled" && live.clarification?.request_id === event.prompt_id) {
    next.clarification = null;
    next.status = "running";
  }
  if (
    (type === "message.delta" || type === "assistant.delta") &&
    typeof event.delta === "string"
  ) {
    next.text = (next.text || "") + event.delta;
    next.statusText = "";
    next.parts = appendText(responseParts(live), event.delta);
  }
  if (type === "message.interim" && typeof event.text === "string" && event.text.trim()) {
    next.parts = interimText(live, event.text, event.already_streamed === true);
    if (event.already_streamed !== true) next.text = (live.text || "") + event.text;
    next.statusText = "";
  }
  if (type === "reasoning.available" && typeof event.text === "string") {
    next.reasoning = event.text;
    const parts = responseParts(live);
    // Hermes can report reasoning after the call's text deltas. Place it
    // before that prose without splitting its still-growing Markdown block.
    const tail = parts.at(-1)?.kind === "text" ? parts.slice(-1) : [];
    const prefix = tail.length ? parts.slice(0, -1) : parts;
    next.parts = [
      ...(prefix.at(-1)?.kind === "reasoning" ? prefix.slice(0, -1) : prefix),
      { kind: "reasoning", text: event.text },
      ...tail,
    ];
  }
  if (type === "tool.started" || type === "tool.start") {
    next.statusText = "";
    next.tools = [...(live.tools || [])];
    const tool = {
      id: event.tool_call_id || `tool-${next.tools.length}`,
      name: event.tool || event.name,
      subject: event.preview || "",
      status: "running",
      kind: "tool",
      startedAt: event.timestamp,
    };
    const index = next.tools.findIndex(
      (t) => t.kind !== "agent" && t.id === tool.id,
    );
    if (index < 0) {
      next.tools.push(tool);
      next.parts = [
        ...responseParts(live),
        { kind: "tool", id: tool.id, agent: false },
      ];
    } else next.tools[index] = { ...next.tools[index], ...tool };
  }
  if (type === "tool.completed" || type === "tool.complete") {
    next.tools = [...(live.tools || [])];
    const index = next.tools.findIndex(
      (t) =>
        t.kind !== "agent" &&
        t.status === "running" &&
        (event.tool_call_id != null
          ? t.id === event.tool_call_id
          : t.name === (event.tool || event.name)),
    );
    if (index >= 0)
      next.tools[index] = {
        ...next.tools[index],
        status: event.error ? "failed" : "completed",
        duration: event.duration,
        endedAt: event.timestamp,
        ...(typeof event.preview === "string" ? { output: event.preview } : {}),
      };
  }
  if (type === "subagent.start" || type === "subagent.complete") {
    next.tools = [...(live.tools || [])];
    const id =
      event.subagent_id ??
      event.child_session_id ??
      event.task_index ??
      "agent";
    const index = next.tools.findIndex(
      (t) => t.kind === "agent" && t.id === id,
    );
    const tool = {
      id,
      kind: "agent",
      task_index: event.task_index,
      name:
        event.goal ||
        event.preview ||
        next.tools[index]?.name ||
        "",
      summary: event.summary || event.output_tail || "",
      status: type.endsWith("start") ? "running" : event.status || "completed",
    };
    for (const field of [
      "child_session_id",
      "model",
      "duration_seconds",
      "cost_usd",
      "input_tokens",
      "output_tokens",
      "reasoning_tokens",
      "api_calls",
      "files_read",
      "files_written",
    ])
      if (event[field] !== undefined && event[field] !== null)
        tool[field] = event[field];
    if (index >= 0) next.tools[index] = { ...next.tools[index], ...tool };
    else {
      next.tools.push(tool);
      next.parts = [
        ...responseParts(live),
        { kind: "tool", id: tool.id, agent: true },
      ];
    }
  }
  if (type === "approval.request") {
    next.approval = nativePrompt(event);
    next.status = "waiting_for_approval";
  }
  if ((type === "approval.responded" || type === "approval.settled") && live.approval &&
    (event.request_id ?? event.prompt_id) === live.approval.request_id) {
    next.approval = null;
    next.status = "running";
  }
  if (type === "run.queued")
    next.statusText = msg("Waiting for an earlier response in this conversation.");
  if (type?.startsWith("run.") && terminal.has(type.slice(4))) {
    next.status = type.slice(4);
    next.outcomeUnknown = event.outcome_unknown === true || next.status === "unknown";
    if (next.status === "unknown") next.uncertain = true;
    if (typeof event.output === "string" && event.output) {
      next.parts = finishText(live, event.output);
      next.text = next.parts
        .filter((part) => part.kind === "text")
        .map((part) => part.text)
        .join("\n\n");
    }
    next.tools = (live.tools || []).map((tool) =>
      tool.status === "running"
        ? {
            ...tool,
            status: next.status === "completed" || next.outcomeUnknown
              ? "not_reported" : next.status,
          }
        : tool,
    );
    next.usage = event.usage;
    next.turnLimitReached = next.status === "failed" && turnLimitReached(event);
    next.error = runError(next.status, event);
    next.approval = null;
    next.clarification = null;
    next.statusText = "";
    next.reconnecting = false;
    next.pendingSteer = event.pending_steer;
    // Guidance that arrived after the last step was never delivered.
    if (typeof event.pending_steer === "string" && event.pending_steer)
      next.parts = responseParts(next).filter((part) =>
        part.kind !== "guidance" || !event.pending_steer.includes(part.text));
    next.needsHistory =
      !(typeof event.output === "string" && event.output) &&
      (live.needsHistory || event.needs_history);
  }
  return next;
}

export async function sendMessage(
  text,
  model = null,
  { images = [], reasoning = "auto" } = {},
) {
  let sid = state.active;
  const existingSession = !!sid;
  const generation = navigationVersion();
  if (state.loading)
    throw new Error(msg("Wait for this session to load before sending."));
  // Capture the originating transcript before image storage or submission can
  // yield to navigation into another session.
  const history = sid ? state.history : [];
  const previousUser = history.findLast(userMessage);
  if (state.readOnlyParent)
    throw new Error(msg("Return to the parent session to continue."));
  if (Object.hasOwn(recoveryRecords, sid))
    throw new Error(
      msg("This session is reconnecting. Wait a moment before sending."),
    );
  if (state.lives[sid]?.uncertain) {
    await retrySubmission(sid);
    return sid;
  }
  if (running(sid)) {
    if (images.length)
      throw new Error(msg("Images can be sent after this response finishes."));
    const live = state.lives[sid];
    if (!live.id)
      throw new Error(msg("Wait for the current message to finish sending."));
    if (live.clarification) {
      await answerClarification(sid, live.clarification.request_id, text);
      return sid;
    }
    // Hermes refuses guidance until the approval is answered.
    if (live.approval)
      throw new Error(msg("Answer the approval request before sending guidance."));
    await api(`/runs/${encodeURIComponent(live.id)}/steer`, {
      method: "POST",
      body: { input: text },
    });
    sentGuidance.set(live.id, [...(sentGuidance.get(live.id) || []), text.trim()]);
    // Hermes saves the guidance after the current step; show it there.
    const current = state.lives[sid];
    if (current?.id === live.id && running(sid))
      publish(sid, { ...current,
        parts: [...responseParts(current), { kind: "guidance", text: text.trim() }] });
    toast(msg("Guidance sent. Hermes will read it at the next tool boundary."));
    return sid;
  }
  if (!sid) {
    const result = await api("/sessions", {
      method: "POST",
      body: {},
    });
    sid = result.id || result.session_id || result.session?.id;
    if (!sid) throw new Error(msg("Hermes did not return a session ID."));
    chooseModel(model, sid);
    chooseReasoning(reasoning, sid);
    writeStorage(`draft.${sid}`, text);
    if (images.length) await pendingStorage(`draft.${sid}`, images);
    if (generation === navigationVersion()) {
      writeStorage("draft.new", "");
      pendingStorage("draft.new", null).catch(() => {});
      update({
        active: sid,
        history: [],
        historyHasMore: false,
        historyOffset: 0,
        sessionDetails: null,
        draftModel: null,
        draftReasoning: "auto",
      });
      rememberSession(sid, true);
    }
    refreshSessions().catch(fail);
  }
  const requestId = randomId();
  const requestedModel = model?.inherited ? null : model;
  const payload = {
    input: text,
    session_id: sid,
    live_interactions: true,
    request_id: requestId,
    ...(requestedModel ? { model: requestedModel.id, provider: requestedModel.provider } : {}),
    ...(existingSession && !model && Object.hasOwn(state.modelChoices, sid) && state.modelChoices[sid] === null
      ? { use_default_model: true } : {}),
    ...(reasoning !== "auto" ? { reasoning } : {}),
    ...(images.length ? { images: images.map(({ url }) => ({ url })) } : {}),
  };
  let imageBoundary = null;
  if (images.length) {
    const before = await api(`/sessions/${encodeURIComponent(sid)}/messages`);
    imageBoundary = before.data?.length ? before.data.at(-1)?.id : 0;
    await pendingStorage(receiptKey(sid, requestId), {
      payload,
      images,
      imageBoundary,
    });
  }
  const live = {
    id: null,
    requestId,
    userText: text,
    baseHistoryLength: history.length,
    // A full history page can retain its length after this message is saved.
    baseUserId: previousUser ? previousUser.id : null,
    baseMessageId: history.at(-1)?.id ?? 0,
    userImages: images,
    imageBoundary,
    imageReceipt: images.length > 0,
    text: "",
    tools: [],
    status: "starting",
    payload,
    createdAt: Date.now(),
  };
  publish(sid, live);
  remember();
  try {
    const result = await api("/runs", { method: "POST", body: payload });
    publish(sid, { ...state.lives[sid], id: runID(result), status: "running" });
    remember();
    subscribe(sid);
    return sid;
  } catch (error) {
    const failed = {
      ...live,
      status: "failed",
      error: error.message,
      uncertain:
        error.status === 0 ||
        error.status >= 500 ||
        error.name === "AbortError",
    };
    failed.submissionFailed = canRetry(failed);
    if (failed.uncertain && !failed.submissionFailed)
      failed.error = receiptError;
    if (!failed.uncertain) {
      failed.imageReceipt = false;
      pendingStorage(receiptKey(sid, requestId), null).catch(() => {});
    }
    publish(sid, failed);
    remember();
    // The reply area shows this failure, with Retry when that is safe.
    error.shownInReply = true;
    throw error;
  }
}

export async function retrySubmission(sid) {
  const live = state.lives[sid];
  if (!live?.payload || live.id) throw new Error(receiptError);
  if (live.retrying) return;
  if (!canRetry(live)) throw new Error(receiptError);
  publish(sid, { ...live, retrying: true });
  try {
    const result = await api("/runs", { method: "POST", body: live.payload });
    const id = runID(result);
    const draftKey = `draft.${sid}`;
    if (readStorage(draftKey) === live.userText) writeStorage(draftKey, "");
    publish(sid, {
      ...live,
      id,
      status: "running",
      error: null,
      submissionFailed: false,
      uncertain: false,
      retrying: false,
      recovered: true,
      // Keep identities after settlement releases the larger image originals.
      recoveredImageIds: (live.userImages || []).map((image) => image.id),
    });
    consumePendingImages(draftKey, live.userImages || []).catch(() => {});
    remember();
    subscribe(sid);
  } catch (error) {
    const failed = {
      ...state.lives[sid],
      retrying: false,
      uncertain:
        live.uncertain ||
        error.status === 0 ||
        error.status >= 500 ||
        error.name === "AbortError",
    };
    failed.submissionFailed = canRetry(failed);
    publish(sid, failed);
    remember();
    throw error;
  }
}

// Over HTTP/1.1 a browser allows six connections to a server across all its
// tabs, and each stream holds one. More streams would stall every other
// request, so only the session on screen streams. Others are checked now and then.
function onScreen(sid) {
  return !streamsUseConnections() ||
    (sid === state.active && document.visibilityState === "visible");
}

export function subscribe(sid, finishing = false) {
  sources.get(sid)?.close();
  sources.delete(sid);
  parkedRuns.delete(sid);
  const live = state.lives[sid];
  if (!live?.id || terminal.has(live.status)) return;
  if (!finishing && !onScreen(sid)) {
    parkedRuns.add(sid);
    checkParkedLater();
    return;
  }
  const cursor = cursors.get(sid);
  const after = cursor?.id === live.id ? `?after=${cursor.after}` : "";
  const source = eventSource(`/runs/${encodeURIComponent(live.id)}/events${after}`);
  source.finishing = finishing;
  sources.set(sid, source);
  const current = () =>
    sources.get(sid) === source && state.lives[sid]?.id === live.id;
  source.onmessage = (message) => {
    if (!current()) return;
    const seen = Number(message.lastEventId);
    if (message.lastEventId && Number.isSafeInteger(seen))
      cursors.set(sid, { id: live.id, after: seen });
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    if (!event || typeof event !== "object" || Array.isArray(event)) return;
    if (event.event === "talaria.reconcile") {
      // A replay that restarts the response rebuilds it, never appends to the
      // copy already shown. Otherwise only the saved transcript is complete.
      publish(sid, {
        ...state.lives[sid],
        ...(event.replay === true ? { text: "", reasoning: "", parts: [], tools: [] } : {}),
        reconnecting: false,
        needsHistory: true,
      });
      return;
    }
    const unavailable = event.event === "talaria.unavailable";
    // Hermes forgot the run, so it ended, though Hermes no longer says how.
    const forgotten = unavailable && event.forgotten === true;
    if (unavailable) {
      event = {
        event: "run.interrupted",
        error: event.message,
        needs_history: true,
        outcome_unknown: true,
      };
    }
    const next = applyEvent(state.lives[sid], event);
    if (forgotten) next.expired = true;
    publish(sid, next);
    if (terminal.has(next.status)) {
      source.close();
      sources.delete(sid);
      cursors.delete(sid);
      remember();
      settle(sid, next).catch(fail);
    }
  };
  source.onopen = () => {
    if (current()) publish(sid, { ...state.lives[sid], reconnecting: false });
  };
  source.onerror = () => {
    if (current() && !terminal.has(state.lives[sid].status))
      publish(sid, { ...state.lives[sid], reconnecting: true });
  };
}

// Stream the session that came on screen and park the ones that left it.
export function refreshStreams() {
  for (const [sid, source] of sources)
    if (!source.finishing && !onScreen(sid)) {
      source.close();
      sources.delete(sid);
      parkedRuns.add(sid);
    }
  if (parkedRuns.has(state.active) && onScreen(state.active)) subscribe(state.active);
  checkParkedLater();
}

function parked(sid) {
  const live = state.lives[sid];
  return parkedRuns.has(sid) && !!live?.id && !terminal.has(live.status) && !sources.has(sid);
}

function checkParkedLater() {
  for (const sid of parkedRuns) if (!parked(sid)) parkedRuns.delete(sid);
  if (!parkedCheck && parkedRuns.size) parkedCheck = setTimeout(checkParked, 4000);
}

async function checkParked() {
  try {
    for (const sid of [...parkedRuns]) {
      const live = state.lives[sid];
      if (!parked(sid)) continue;
      let status;
      try {
        status = await api(`/runs/${encodeURIComponent(live.id)}`);
      } catch (error) {
        // The stream reports a run Hermes no longer has.
        if (error.status === 404 && parked(sid) && state.lives[sid].id === live.id)
          subscribe(sid, true);
        continue;
      }
      const current = state.lives[sid];
      if (current?.id !== live.id || !parked(sid) || typeof status?.status !== "string")
        continue;
      // A finished run's stream delivers the rest of its events, then closes.
      if (terminal.has(status.status)) subscribe(sid, true);
      else if (status.status !== current.status && current.status !== "stopping")
        publish(sid, { ...current, status: status.status });
    }
  } finally {
    parkedCheck = null;
    checkParkedLater();
  }
}

export function responseSaved(history, live) {
  // Hermes can persist its summary request as another user row. Its turn-limit
  // handoff still belongs to this run: match the authoritative final output
  // beyond the originating message boundary before checking the latest user.
  if (live.turnLimitReached) {
    const final = history.findLast((message) =>
      message.role === "assistant" && message.display_kind !== "hidden");
    const tail = responseParts(live).findLast((part) => part.kind === "text")?.text;
    if (final && tail?.trim() && plainContent(final.content).trim() === tail.trim() &&
      (typeof live.baseMessageId !== "number" || typeof final.id !== "number" ||
        final.id > live.baseMessageId)) return true;
  }
  const lastUser = history.findLastIndex(userMessage);
  const user = history[lastUser];
  if (
    user &&
    withoutImagePlaceholders(plainContent(user.content)) !== live.userText
  )
    return false;
  // Repeated prompts are different turns. A failed save must never adopt the
  // previous response just because the user sent identical text again.
  if (user && live.baseUserId !== undefined && user.id === live.baseUserId)
    return false;
  const final = history.findLast(
    (message, index) =>
      index > lastUser &&
      message.role === "assistant" &&
      message.display_kind !== "hidden" &&
      (message.content ||
        message.tool_calls?.length ||
        message.reasoning ||
        message.reasoning_content),
  );
  if (!final) return false;
  if (typeof live.baseMessageId === "number" && typeof final.id === "number")
    return terminal.has(live.status) && final.id > live.baseMessageId;
  // Older recovery receipts lack a message boundary. Require the originating
  // user row before using text-based reconciliation for those receipts.
  if (!user) return false;
  const content = plainContent(final?.content);
  const tail = responseParts(live).findLast(
    (part) => part.kind === "text",
  )?.text;
  return !!(
    (live.baseUserId !== undefined && terminal.has(live.status)) ||
    ["cancelled", "interrupted"].includes(live.status) ||
    (live.text && content.trim() === live.text.trim()) ||
    (tail && content.trim() === tail.trim()) ||
    (live.needsHistory && live.status === "completed")
  );
}

export async function prepareRunReconciliation(sid, history, current) {
  const live = state.lives[sid];
  if (!current() || !live?.id || !terminal.has(live.status) ||
    (live.persisted && !live.imageReceipt)) return;
  const reply = history.findLast(
    (message) => message.role === "assistant" && message.display_kind !== "hidden",
  );
  const saved = responseSaved(history, live) ? {
    persisted: true,
    savedMessageId: reply?.id,
    // Hermes forgot the run but saved its final answer: it finished, though
    // Hermes no longer says how, so Response details claim no status. A reply
    // that stopped at a tool call or an error still asks for a check.
    ...(live.expired && reply?.finish_reason === "stop" ? {
      status: "completed", error: null, outcomeUnknown: true, uncertain: false, expired: false,
    } : {}),
  } : {};
  const imageMessage = currentImageMessage(history, live);
  let imageSaved = !live.imageReceipt;
  if (imageMessage && placeholderCount(imageMessage.content)) {
    try {
      await cacheMessageImages(
        imageMessage.session_id || sid,
        imageMessage.id,
        live.userImages,
        receiptKey(sid, live.requestId),
        imageFingerprint(imageMessage),
      );
      imageSaved = true;
    } catch (error) {
      toast(error.message);
    }
    imageMessage.browserImages = live.userImages;
  }
  if (
    live.imageReceipt &&
    imageMessage &&
    !placeholderCount(imageMessage.content)
  ) {
    await pendingStorage(receiptKey(sid, live.requestId), null).catch(() => {});
    imageSaved = true;
  }
  if (live.imageReceipt && !imageMessage)
    toast(
      msg("Hermes received the image, but its transcript could not be linked to the original in this browser."),
    );
  // Apply synchronously with the history commit, after all asynchronous work.
  // The view must not see the live copy retire before its saved rows appear.
  return () => {
    if (!current() || state.lives[sid]?.id !== live.id) return;
    publish(sid, {
      ...state.lives[sid],
      ...saved,
      imageReceipt: !imageSaved,
      ...((saved.persisted || imageMessage) && imageSaved
        ? { userImages: [], payload: undefined }
        : {}),
      ...(imageMessage && imageSaved
        ? { userPersisted: true }
        : {}),
    });
    remember();
  };
}

async function settle(sid, live) {
  await refreshHistory(sid, () => state.lives[sid]?.id === live.id);
  if (state.lives[sid]?.id !== live.id) return;
  await refreshSessions();
  refreshSessionDetails(sid).catch(() => {});
  refreshReadiness();
  const mine = (sentGuidance.get(live.id) || []).filter((text) =>
    typeof live.pendingSteer === "string" && live.pendingSteer.includes(text));
  sentGuidance.delete(live.id);
  if (mine.length) restoreGuidance(sid, mine.join("\n\n"));
}

// Guidance each response was sent from this tab. Every device shows guidance
// Hermes never received; only the tab that sent it gets it back to send again.
const sentGuidance = new Map();

// Guidance sent as a response ended never reached Hermes. It returns to the
// message box, ready to send, unless someone is writing there.
function restoreGuidance(sid, text) {
  toast(restoreDraft(sid, text)
    ? msg("Your guidance arrived after the response finished. It is back in the message box.")
    : msg("Your guidance arrived after the response finished. It is shown below."));
}

// Hermes saves a turn's prompt as it starts. Until then, or once the turn has
// a final reply, the open history holds no unanswered turn for this run.
function unansweredPrompt(history, run) {
  const index = history.findLastIndex(userMessage);
  const prompt = history[index];
  if (!prompt || !(Number(prompt.timestamp) >= run.created_at - 5)) return -1;
  const finished = history.slice(index + 1).some((row) => row?.role === "session_meta" ||
    (row?.role === "assistant" && !row.tool_calls?.length && plainContent(row.content).trim()));
  return finished ? -1 : index;
}

function storedRun(sid) {
  return savedRuns()[sid] || null;
}

// Follow a reply that another device or tab is running in the open session.
export function followRun(sid, run) {
  const live = state.lives[sid];
  if (sid !== state.active || running(sid) || live?.uncertain || live?.id === run?.run_id ||
      !["running", "waiting_for_approval", "waiting_for_input"].includes(run?.status) ||
      typeof run.run_id !== "string" || !Number.isFinite(run.created_at) ||
      Object.hasOwn(recoveryRecords, sid)) return false;
  // Another tab of this browser stored the run's own record. This tab's
  // recovery reconnects its own runs and restores their images.
  const stored = storedRun(sid);
  let origin;
  if (stored?.id === run.run_id) {
    if (!recovered || stored.imageReceipt) return false;
    origin = stored;
  } else {
    origin = promptOrigin(state.history, run);
    if (!origin) return false;
  }
  publish(sid, {
    ...origin,
    id: run.run_id,
    text: "",
    tools: [],
    status: run.status,
    createdAt: run.created_at * 1000,
    needsHistory: true,
  });
  remember();
  subscribe(sid);
  return true;
}

function promptOrigin(history, run) {
  const index = unansweredPrompt(history, run);
  if (index < 0) return null;
  const previous = history.slice(0, index).findLast(userMessage);
  const before = history[index - 1]?.id;
  return {
    userText: withoutImagePlaceholders(plainContent(history[index].content)),
    baseUserId: previous ? previous.id : null,
    baseMessageId: typeof before === "number" ? before : 0,
  };
}

// Why a response failed or stopped is part of the conversation on every
// device and after reloads, for as long as Hermes reports the run (an hour).
export async function showOutcome(sid, run) {
  const known = () => {
    const live = state.lives[sid];
    return sid !== state.active || running(sid) || live?.uncertain || live?.id === run.run_id ||
      (Number.isFinite(live?.createdAt) && live.createdAt >= run.created_at * 1000) ||
      Object.hasOwn(recoveryRecords, sid);
  };
  if (!["failed", "cancelled", "interrupted"].includes(run?.status) ||
      typeof run.run_id !== "string" || !Number.isFinite(run.created_at) || known()) return false;
  const history = state.history;
  const origin = promptOrigin(history, run);
  if (!origin) return false;
  let status;
  try {
    status = await api(`/runs/${encodeURIComponent(run.run_id)}`);
  } catch {
    return false;
  }
  if (known() || state.history !== history || !terminal.has(status?.status)) return false;
  publish(sid, {
    ...origin,
    id: run.run_id,
    text: typeof status.output === "string" ? status.output : "",
    tools: [],
    status: status.status,
    createdAt: run.created_at * 1000,
    error: runError(status.status, status),
    turnLimitReached: status.status === "failed" && turnLimitReached(status),
    usage: status.usage,
  });
  return true;
}

export function restoreRuns() {
  if (!restoration)
    restoration = recoverRuns().finally(() => {
      restoration = null;
      recovered = true;
    });
  return restoration;
}

async function recoverRuns() {
  recoveryRecords = Object.fromEntries(Object.entries(savedRuns()).slice(-16));
  const records = Object.entries(recoveryRecords).sort(
    ([a], [b]) => Number(b === state.active) - Number(a === state.active),
  );
  async function recover([sid, savedRecord]) {
    const record = { ...savedRecord };
    try {
      if (Object.hasOwn(state.lives, sid)) return;
      if (record.imageReceipt) {
        const receipt =
          (await pendingStorage(receiptKey(sid, record.requestId)).catch(
            () => null,
          )) || (await pendingStorage(`run.${sid}`).catch(() => null));
        if (receipt)
          Object.assign(record, {
            payload: receipt.payload,
            userImages: receipt.images,
            imageBoundary: receipt.imageBoundary,
          });
        else if (!record.id) {
          if (Object.hasOwn(state.lives, sid)) return;
          publish(sid, {
            ...record,
            text: "",
            tools: [],
            status: "failed",
            uncertain: true,
            error: receiptError,
          });
          return;
        }
      }
      if (Object.hasOwn(state.lives, sid)) return;
      if (!record.id && record.payload) {
        if (
          record.payload.session_id !== sid ||
          record.payload.request_id !== record.requestId
        )
          return;
        const live = {
          ...record,
          text: "",
          tools: [],
          status: "failed",
          uncertain: true,
        };
        live.submissionFailed = canRetry(live);
        live.error = live.submissionFailed
          ? msg("The submission confirmation was lost. Retry safely to recover this response.")
          : receiptError;
        publish(sid, live);
        return;
      }
      if (typeof record.id !== "string" || !record.id) return;
      const status = await api(`/runs/${encodeURIComponent(record.id)}`);
      if (Object.hasOwn(state.lives, sid)) return;
      if (!status || typeof status.status !== "string")
        throw new RequestError(
          msg("The response status is unavailable."),
          0,
          "invalid_response",
        );
      const live = {
        ...record,
        createdAt: Number.isFinite(record.createdAt) ? record.createdAt
          : Number.isFinite(status.created_at) ? status.created_at * 1000 : undefined,
        // Active runs replay their deltas. A status snapshot may already
        // contain those same deltas, so only use its output once terminal.
        text: terminal.has(status.status) ? status.output || "" : "",
        tools: [],
        status: status.status,
        approval: terminal.has(status.status)
          ? null : status.approval || pendingControl(status, "approval"),
        clarification: terminal.has(status.status)
          ? null : status.clarification || pendingControl(status, "clarify"),
        turnLimitReached: status.status === "failed" && turnLimitReached(status),
        ...(status.status === "unknown" ? { uncertain: true, outcomeUnknown: true } : {}),
        error: runError(status.status, status),
        usage: status.usage,
        pendingSteer: status.pending_steer,
        needsHistory:
          !terminal.has(status.status) ||
          !(typeof status.output === "string" && status.output),
      };
      publish(sid, live);
      if (!terminal.has(live.status)) subscribe(sid);
      else await settle(sid, live);
    } catch (error) {
      if (!Object.hasOwn(state.lives, sid) && typeof record.id === "string") {
        const live = {
          ...record,
          text: "",
          tools: [],
          status: "interrupted",
          uncertain: error.status !== 404,
          expired: error.status === 404,
          needsHistory: true,
          error:
            error.status === 404
              ? msg("Live updates expired. Check the session for the response.")
              : msg("Could not check this response. Reload to reconnect before sending another message."),
        };
        publish(sid, live);
        if (error.status === 404) await settle(sid, live).catch(() => {});
      }
    } finally {
      delete recoveryRecords[sid];
      const key = runKey(savedRecord);
      if (key) recoveredRuns.add(key);
    }
  }
  // Prioritize the open session and keep a small number of independent
  // checks in flight. One slow status request must not stall every restored response.
  let cursor = 0;
  await Promise.all(
    Array.from({ length: Math.min(4, records.length) }, async () => {
      while (cursor < records.length) await recover(records[cursor++]);
    }),
  );
  remember();
}

export async function stopRun(sid) {
  const id = state.lives[sid]?.id;
  if (!id || !running(sid)) return;
  await api(`/runs/${encodeURIComponent(id)}/stop`, {
    method: "POST",
    body: {},
  });
  if (state.lives[sid]?.id === id && running(sid))
    publish(sid, { ...state.lives[sid], status: "stopping" });
}

// Hermes never runs a discarded turn again; queued turns behind it then continue.
export async function discardRun(sid) {
  const live = state.lives[sid];
  if (live?.status !== "unknown" || live.discarding) return;
  publish(sid, { ...live, discarding: true });
  try {
    const status = await api(`/runs/${encodeURIComponent(live.id)}/discard`, {
      method: "POST",
      body: {},
    });
    if (state.lives[sid]?.id !== live.id) return;
    if (typeof status?.status !== "string" || !terminal.has(status.status) ||
      status.status === "unknown")
      throw new Error(msg("Hermes could not discard this response. Try again."));
    const next = {
      ...applyEvent(state.lives[sid], { ...status, event: `run.${status.status}`, needs_history: true }),
      uncertain: false,
      discarding: false,
      discarded: true,
    };
    publish(sid, next);
    remember();
    await settle(sid, next);
  } catch (error) {
    if (state.lives[sid]?.id === live.id)
      publish(sid, { ...state.lives[sid], discarding: false });
    throw error;
  }
}

export async function approve(sid, choice) {
  const live = state.lives[sid];
  if (!live?.id || !live.approval || !running(sid)) return;
  await api(`/runs/${encodeURIComponent(live.id)}/approval`, {
    method: "POST",
    body: { choice, request_id: live.approval?.request_id, ...generation(live.approval) },
  });
  if (
    state.lives[sid]?.id === live.id &&
    running(sid) &&
    state.lives[sid].approval === live.approval
  )
    publish(sid, { ...state.lives[sid], approval: null, status: "running" });
}

const answering = new Set();
export async function answerClarification(sid, requestId, answer) {
  const live = state.lives[sid];
  if (!live?.id || live.clarification?.request_id !== requestId || !running(sid))
    throw new Error(msg("This question is no longer waiting for an answer."));
  if (answering.has(requestId)) return;
  answering.add(requestId);
  try {
    await api(`/runs/${encodeURIComponent(live.id)}/clarification`, {
      method: "POST",
      body: { request_id: requestId, answer, ...generation(live.clarification) },
    });
    if (state.lives[sid]?.id === live.id && state.lives[sid]?.clarification?.request_id === requestId)
      publish(sid, { ...state.lives[sid], clarification: null, status: "running" });
  } finally {
    answering.delete(requestId);
  }
}
