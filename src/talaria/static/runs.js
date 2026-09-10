import { api, RequestError } from "./api.js";
import { apiURL } from "./profile-context.js";
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
} from "./store.js";
import { readStorage, writeStorage } from "./lib.js";
import {
  pendingStorage,
  cacheMessageImages,
  currentImageMessage,
  placeholderCount,
  withoutImagePlaceholders,
} from "./attachments.js";
import { plainContent } from "./content.js";

const sources = new Map();
let recoveryRecords = {};
let restoration;
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const receiptKey = (sid, requestId) => `run.${sid}.${requestId}`;
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
    for (const [id] of disposable.slice(0, -16)) delete lives[id];
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
        imageReceipt: !!r.imageReceipt,
        ...(!r.id
          ? {
              ...(r.payload?.images?.length
                ? { imageReceipt: true }
                : { payload: r.payload }),
              createdAt: r.createdAt,
            }
          : {}),
      },
    ]);
  const saved = { ...recoveryRecords, ...Object.fromEntries(entries) };
  writeStorage(
    "runs",
    JSON.stringify(Object.fromEntries(Object.entries(saved).slice(-16))),
  );
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
  "The submission confirmation was lost. Check the conversation before sending another message.";
export const running = (sid, lives = state.lives) =>
  Object.hasOwn(lives, sid) && !!lives[sid] && !terminal.has(lives[sid].status);

function runID(result) {
  if (typeof result?.run_id === "string" && result.run_id) return result.run_id;
  throw new RequestError(
    "The submission confirmation was incomplete.",
    0,
    "invalid_response",
  );
}

export function clearCompletedRun(sid) {
  if (running(sid) || state.lives[sid]?.uncertain)
    throw new Error(
      "Wait for the current response to finish before changing this turn.",
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
  if (
    (type === "message.delta" || type === "assistant.delta") &&
    typeof event.delta === "string"
  )
    next.text = (next.text || "") + event.delta;
  if (type === "reasoning.available" && typeof event.text === "string")
    next.reasoning = event.text;
  if (type === "tool.started" || type === "tool.start") {
    next.tools = [...(live.tools || [])];
    const tool = {
      id: event.tool_call_id || `tool-${next.tools.length}`,
      name: event.tool || event.name,
      preview: event.preview || "",
      status: "running",
      kind: "tool",
    };
    const index = next.tools.findIndex(
      (t) => t.kind !== "agent" && t.id === tool.id,
    );
    if (index < 0) next.tools.push(tool);
    else next.tools[index] = { ...next.tools[index], ...tool };
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
        "Delegated task",
      preview: event.summary || event.output_tail || "",
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
    else next.tools.push(tool);
  }
  if (type === "approval.request") {
    next.approval = event;
    next.status = "waiting_for_approval";
  }
  if (type?.startsWith("run.") && terminal.has(type.slice(4))) {
    next.status = type.slice(4);
    if (typeof event.output === "string" && event.output)
      next.text = event.output;
    next.usage = event.usage;
    next.error = event.error;
    next.approval = null;
    next.reconnecting = false;
    next.pendingSteer = event.pending_steer;
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
  const generation = navigationVersion();
  if (state.readOnlyParent)
    throw new Error("Return to the parent conversation to continue.");
  if (Object.hasOwn(recoveryRecords, sid))
    throw new Error(
      "This conversation is reconnecting. Wait a moment before sending.",
    );
  if (state.lives[sid]?.uncertain) {
    await retrySubmission(sid);
    return sid;
  }
  if (running(sid)) {
    if (images.length)
      throw new Error("Images can be sent after this response finishes.");
    const live = state.lives[sid];
    if (!live.id)
      throw new Error("Wait for the current message to finish sending.");
    await api(`/runs/${encodeURIComponent(live.id)}/steer`, {
      method: "POST",
      body: { input: text },
    });
    toast("Guidance sent. Hermes will read it at the next tool boundary.");
    return sid;
  }
  if (!sid) {
    const result = await api("/sessions", {
      method: "POST",
      body: {
        title: text.replace(/\s+/g, " ").slice(0, 70) || "Image conversation",
      },
    });
    sid = result.id || result.session_id || result.session?.id;
    if (!sid) throw new Error("Hermes did not return a conversation ID.");
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
      writeStorage("last-session", sid);
    }
    refreshSessions().catch(fail);
  }
  const requestId = crypto.randomUUID();
  const payload = {
    input: text,
    session_id: sid,
    request_id: requestId,
    ...(model ? { model: model.id, provider: model.provider } : {}),
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
  const previousUser = state.history.findLast(
    (message) => message.role === "user",
  );
  const live = {
    id: null,
    requestId,
    userText: text,
    baseHistoryLength: state.history.length,
    // A full history page can retain its length after this message is saved.
    baseUserId: previousUser ? previousUser.id : null,
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
    publish(sid, {
      ...live,
      id: runID(result),
      status: "running",
      error: null,
      submissionFailed: false,
      uncertain: false,
      retrying: false,
      recovered: true,
    });
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

export function subscribe(sid) {
  sources.get(sid)?.close();
  sources.delete(sid);
  const live = state.lives[sid];
  if (!live?.id || terminal.has(live.status)) return;
  const source = new EventSource(
    apiURL(`/runs/${encodeURIComponent(live.id)}/events`),
  );
  sources.set(sid, source);
  const current = () =>
    sources.get(sid) === source && state.lives[sid]?.id === live.id;
  source.onmessage = (message) => {
    if (!current()) return;
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    if (!event || typeof event !== "object" || Array.isArray(event)) return;
    if (event.event === "talaria.reconcile") {
      publish(sid, {
        ...state.lives[sid],
        reconnecting: true,
        needsHistory: true,
      });
      return;
    }
    if (event.event === "talaria.unavailable") {
      source.close();
      sources.delete(sid);
      publish(sid, {
        ...state.lives[sid],
        status: "interrupted",
        reconnecting: false,
        approval: null,
        error: event.message,
      });
      refreshHistory(sid).catch(fail);
      remember();
      return;
    }
    const next = applyEvent(state.lives[sid], event);
    publish(sid, next);
    if (terminal.has(next.status)) {
      source.close();
      sources.delete(sid);
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

function responseSaved(history, live) {
  const lastUser = history.findLastIndex((message) => message.role === "user");
  const final = history.findLast(
    (message, index) =>
      index > lastUser && message.role === "assistant" && message.content,
  );
  const content = plainContent(final?.content);
  return !!(
    content &&
    withoutImagePlaceholders(plainContent(history[lastUser]?.content)) ===
      live.userText &&
    ((live.text && content.trim() === live.text.trim()) ||
      (live.needsHistory && live.status === "completed"))
  );
}

async function settle(sid, live) {
  const history = await refreshHistory(
    sid,
    () => state.lives[sid]?.id === live.id,
    (history) => responseSaved(history, live)
      ? { lives: { ...state.lives, [sid]: { ...state.lives[sid], persisted: true } } }
      : {},
  );
  const imageMessage = currentImageMessage(history, live);
  let imageSaved = !live.imageReceipt;
  if (imageMessage && placeholderCount(imageMessage.content)) {
    try {
      await cacheMessageImages(
        imageMessage.session_id || sid,
        imageMessage.id,
        live.userImages,
        receiptKey(sid, live.requestId),
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
      "Hermes received the image, but its transcript could not be linked to the original in this browser.",
    );
  if (state.lives[sid]?.id !== live.id) return;
  if (state.history === history) update({ history: [...history] }, true);
  publish(sid, {
    ...state.lives[sid],
    imageReceipt: !imageSaved,
    ...(imageMessage && imageSaved
      ? { userImages: [], userPersisted: true, payload: undefined }
      : {}),
  });
  if (responseSaved(history, live)) {
    publish(sid, {
      ...state.lives[sid],
      persisted: true,
      ...(imageSaved ? { userImages: [], payload: undefined } : {}),
    });
  }
  remember();
  await refreshSessions();
  refreshSessionDetails(sid).catch(() => {});
  refreshReadiness();
  if (live.pendingSteer)
    toast("Some guidance arrived after the response. It is available below.");
}

export function restoreRuns() {
  if (!restoration)
    restoration = recoverRuns().finally(() => {
      restoration = null;
    });
  return restoration;
}

async function recoverRuns() {
  let saved;
  try {
    saved = JSON.parse(readStorage("runs", "{}"));
  } catch {
    return;
  }
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return;
  recoveryRecords = Object.fromEntries(
    Object.entries(saved)
      .filter(
        ([, record]) =>
          record && typeof record === "object" && !Array.isArray(record),
      )
      .slice(-16),
  );
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
          ? "The submission confirmation was lost. Retry safely to recover this response."
          : receiptError;
        publish(sid, live);
        return;
      }
      if (typeof record.id !== "string" || !record.id) return;
      const status = await api(`/runs/${encodeURIComponent(record.id)}`);
      if (Object.hasOwn(state.lives, sid)) return;
      if (!status || typeof status.status !== "string")
        throw new RequestError(
          "The response status is unavailable.",
          0,
          "invalid_response",
        );
      const live = {
        ...record,
        // Active runs replay their deltas. A status snapshot may already
        // contain those same deltas, so only use its output once terminal.
        text: terminal.has(status.status) ? status.output || "" : "",
        tools: [],
        status: status.status,
        approval: terminal.has(status.status) ? null : status.approval,
        error: status.error,
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
          needsHistory: true,
          error:
            error.status === 404
              ? "Live updates expired. Check the conversation for the response."
              : "Could not check this response. Reload to reconnect before sending another message.",
        };
        publish(sid, live);
        if (error.status === 404) await settle(sid, live).catch(() => {});
      }
    } finally {
      delete recoveryRecords[sid];
    }
  }
  // Prioritize the open conversation and keep a small number of independent
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

export async function approve(sid, choice) {
  const live = state.lives[sid];
  if (!live?.id || !live.approval || !running(sid)) return;
  await api(`/runs/${encodeURIComponent(live.id)}/approval`, {
    method: "POST",
    body: { choice, request_id: live.approval?.request_id },
  });
  if (
    state.lives[sid]?.id === live.id &&
    running(sid) &&
    state.lives[sid].approval === live.approval
  )
    publish(sid, { ...state.lives[sid], approval: null, status: "running" });
}
