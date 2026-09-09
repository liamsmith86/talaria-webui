import { api } from "./api.js";
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
import { pendingStorage } from "./attachments.js";
import { plainContent } from "./content.js";

const sources = new Map();
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
function publish(sid, live) {
  update({ lives: { ...state.lives, [sid]: live } }, true);
}
function remember() {
  const entries = Object.entries(state.lives)
    .filter(([, r]) => !terminal.has(r.status) || r.uncertain)
    .slice(-16)
    .map(([sid, r]) => [
      sid,
      {
        id: r.id,
        requestId: r.requestId,
        userText: r.userText,
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
  writeStorage("runs", JSON.stringify(Object.fromEntries(entries)));
}
function canRetry(live) {
  if (!live.uncertain) return true;
  const protection = state.caps.features?.runs_idempotency;
  const retention = Math.min(protection?.retention_seconds || 0, 86400) * 1000;
  return (
    protection?.supported === true &&
    retention > 0 &&
    Date.now() - live.createdAt < retention
  );
}
const receiptError =
  "The submission confirmation was lost. Check the conversation before sending another message.";
export const running = (sid, lives = state.lives) =>
  !!lives[sid] && !terminal.has(lives[sid].status);

export function applyEvent(live, event) {
  const next = { ...live, tools: [...(live.tools || [])] };
  const type = event.event || event.type;
  if (typeof type !== "string") return next;
  if (type === "message.delta" || type === "assistant.delta")
    next.text += event.delta || "";
  if (type === "reasoning.available") next.reasoning = event.text || "";
  if (type === "tool.started" || type === "tool.start") {
    next.tools.push({
      id: event.tool_call_id || `tool-${next.tools.length}`,
      name: event.tool || event.name,
      preview: event.preview || "",
      status: "running",
      kind: "tool",
    });
  }
  if (type === "tool.completed" || type === "tool.complete") {
    const index = next.tools.findIndex(
      (t) =>
        t.status === "running" &&
        (t.name === event.tool || t.id === event.tool_call_id),
    );
    if (index >= 0)
      next.tools[index] = {
        ...next.tools[index],
        status: event.error ? "failed" : "completed",
        duration: event.duration,
      };
  }
  if (type === "subagent.start" || type === "subagent.complete") {
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
    next.text = event.output || next.text;
    next.usage = event.usage;
    next.error = event.error;
    next.approval = null;
    next.pendingSteer = event.pending_steer;
    next.needsHistory = live.needsHistory || event.needs_history;
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
  if (state.lives[sid]?.uncertain) {
    await retrySubmission(sid);
    return sid;
  }
  if (running(sid)) {
    if (images.length)
      throw new Error("Images can be sent after this response finishes.");
    const live = state.lives[sid];
    await api(`/runs/${live.id}/steer`, {
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
  if (images.length) await pendingStorage(`run.${sid}`, { payload, images });
  const live = {
    id: null,
    requestId,
    userText: text,
    baseHistoryLength: state.history.length,
    userImages: images,
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
    publish(sid, { ...state.lives[sid], id: result.run_id, status: "running" });
    remember();
    if (images.length) pendingStorage(`run.${sid}`, null).catch(() => {});
    subscribe(sid);
    return sid;
  } catch (error) {
    const failed = {
      ...live,
      status: "failed",
      error: error.message,
      uncertain: error.status === 0 || error.status >= 500,
    };
    failed.submissionFailed = canRetry(failed);
    if (failed.uncertain && !failed.submissionFailed)
      failed.error = receiptError;
    if (!failed.uncertain) pendingStorage(`run.${sid}`, null).catch(() => {});
    publish(sid, failed);
    remember();
    throw error;
  }
}

export async function retrySubmission(sid) {
  const live = state.lives[sid];
  if (live.retrying) return;
  if (!canRetry(live)) throw new Error(receiptError);
  publish(sid, { ...live, retrying: true });
  try {
    const result = await api("/runs", { method: "POST", body: live.payload });
    publish(sid, {
      ...live,
      id: result.run_id,
      status: "running",
      error: null,
      submissionFailed: false,
      uncertain: false,
      retrying: false,
      recovered: true,
    });
    remember();
    pendingStorage(`run.${sid}`, null).catch(() => {});
    subscribe(sid);
  } catch (error) {
    const failed = {
      ...state.lives[sid],
      retrying: false,
      uncertain: live.uncertain || error.status === 0 || error.status >= 500,
    };
    failed.submissionFailed = canRetry(failed);
    publish(sid, failed);
    remember();
    throw error;
  }
}

export function subscribe(sid) {
  sources.get(sid)?.close();
  const live = state.lives[sid];
  if (!live?.id) return;
  const source = new EventSource(
    `/api/runs/${encodeURIComponent(live.id)}/events`,
  );
  sources.set(sid, source);
  source.onmessage = (message) => {
    if (state.lives[sid]?.id !== live.id) return;
    let event;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
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
    if (state.lives[sid])
      publish(sid, { ...state.lives[sid], reconnecting: false });
  };
  source.onerror = () => {
    if (state.lives[sid] && !terminal.has(state.lives[sid].status))
      publish(sid, { ...state.lives[sid], reconnecting: true });
  };
}

async function settle(sid, live) {
  const history = await refreshHistory(sid);
  const final = [...history]
    .reverse()
    .find((m) => m.role === "assistant" && m.content);
  const content = plainContent(final?.content);
  const lastUser = [...history].reverse().find((m) => m.role === "user");
  if (
    content &&
    ((live.text && content.trim() === live.text.trim()) ||
      (live.needsHistory && plainContent(lastUser?.content) === live.userText))
  ) {
    publish(sid, {
      ...state.lives[sid],
      persisted: true,
      userImages: [],
      payload: undefined,
    });
  }
  await refreshSessions();
  refreshSessionDetails(sid).catch(() => {});
  refreshReadiness();
  if (live.pendingSteer)
    toast("Some guidance arrived after the response. It is available below.");
}

export async function restoreRuns() {
  let saved;
  try {
    saved = JSON.parse(readStorage("runs", "{}"));
  } catch {
    return;
  }
  for (const [sid, record] of Object.entries(saved).slice(0, 16)) {
    try {
      if (record.imageReceipt) {
        const receipt = await pendingStorage(`run.${sid}`).catch(() => null);
        if (receipt)
          Object.assign(record, {
            payload: receipt.payload,
            userImages: receipt.images,
          });
        else {
          publish(sid, {
            ...record,
            text: "",
            tools: [],
            status: "failed",
            uncertain: true,
            error: receiptError,
          });
          continue;
        }
      }
      if (!record.id && record.payload) {
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
        continue;
      }
      const status = await api(`/runs/${encodeURIComponent(record.id)}`);
      const live = {
        ...record,
        text: status.output || "",
        tools: [],
        status: status.status,
        approval: status.approval,
      };
      publish(sid, live);
      if (!terminal.has(live.status)) subscribe(sid);
      else await settle(sid, live);
    } catch {
      /* Hermes history remains available if run metadata has expired. */
    }
  }
  remember();
}

export async function stopRun(sid) {
  const id = state.lives[sid].id;
  await api(`/runs/${id}/stop`, { method: "POST", body: {} });
  if (state.lives[sid]?.id === id && running(sid))
    publish(sid, { ...state.lives[sid], status: "stopping" });
}

export async function approve(sid, choice) {
  const live = state.lives[sid];
  await api(`/runs/${live.id}/approval`, {
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
