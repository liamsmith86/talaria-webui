import { useEffect, useState, readStorage, writeStorage } from "./lib.js";
import { api, setCSRF } from "./api.js";
import { withCachedImages } from "./attachments.js";
import {
  modelInventory,
  readModelChoices,
  saveModelChoice,
  readReasoningChoices,
  saveReasoningChoice,
} from "./models.js";

const listeners = new Set();
export const state = {
  auth: null,
  connected: false,
  connecting: false,
  sessions: [],
  hasMore: false,
  sessionsOffset: 0,
  active: null,
  history: [],
  historyHasMore: false,
  historyOffset: 0,
  sessionDetails: null,
  readOnlyParent: null,
  findOpen: false,
  loading: false,
  lives: {},
  caps: {},
  models: [],
  providers: [],
  defaultModel: null,
  modelChoices: readModelChoices(),
  draftModel: null,
  reasoningChoices: readReasoningChoices(),
  draftReasoning: "auto",
  readiness: { status: "unknown", issues: [] },
  agent: { name: "Hermes", name_source: "fallback" },
  agentInfo: null,
  version: "",
  environment: "production",
  profiles: [],
  profile: null,
  profileMissing: false,
  modal: null,
  sidebar: false,
  error: "",
  toast: "",
};
let frame = 0;
export function update(changes, defer = false) {
  Object.assign(state, changes);
  if (defer) {
    if (!frame)
      frame = requestAnimationFrame(() => {
        frame = 0;
        update({});
      });
    return;
  }
  cancelAnimationFrame(frame);
  frame = 0;
  for (const listener of listeners) listener({ ...state });
}
export function useStore() {
  const [snapshot, set] = useState({ ...state });
  useEffect(() => {
    listeners.add(set);
    // Updates can land between the first render and this effect subscribing.
    set({ ...state });
    return () => listeners.delete(set);
  }, []);
  return snapshot;
}
let toastTimer;
export function toast(message) {
  clearTimeout(toastTimer);
  update({ toast: message });
  toastTimer = setTimeout(() => update({ toast: "" }), 3500);
}
export function fail(error) {
  if (error.code === "unauthenticated") update({ auth: false });
  else if (error.code === "profile_missing") {
    update({
      connected: false,
      profileMissing: true,
      modal: "profiles",
      error: "",
    });
    refreshProfiles().catch(() => {});
  } else
    update({
      error: error.message || "Something went wrong. Please try again.",
    });
}
export async function initialize() {
  try {
    const data = await api("/bootstrap");
    document.title =
      data.environment === "development" ? "Talaria · Dev" : "Talaria";
    setCSRF(data.csrf || "");
    update({
      auth: data.authenticated,
      agent: data.agent || state.agent,
      version: data.version,
      environment: data.environment || "production",
      profiles: data.profiles || [],
      profile: data.profile || null,
    });
    if (data.authenticated) await connect(data.connected);
  } catch (error) {
    if (error.code === "profile_missing") {
      try {
        const data = await api("/profiles");
        setCSRF(data.csrf || "");
        update({
          auth: true,
          connected: false,
          profileMissing: true,
          profiles: data.profiles,
          modal: "profiles",
        });
        return;
      } catch (failure) {
        error = failure;
      }
    }
    fail(error);
    update({ auth: false });
  }
}
export async function refreshProfiles() {
  const data = await api("/profiles");
  update({ profiles: data.profiles });
  return data;
}
export async function connect(configured = true) {
  if (!configured) {
    update({ connected: false, modal: "connection" });
    return;
  }
  update({ connecting: true, error: "" });
  try {
    const caps = await api("/capabilities");
    update({
      caps,
      connected: true,
      connecting: false,
      agent: caps.talaria_agent || state.agent,
    });
    // The sidebar, current transcript, catalog, and readiness are independent.
    // A slow conversation listing must not postpone showing the last reply.
    const sessions = caps.features?.session_resources
      ? refreshSessions()
      : Promise.resolve(update({ sessions: [], hasMore: false, history: [] }));
    if (caps.features?.model_options) {
      refreshModels().catch(() =>
        update({ models: [], providers: [], defaultModel: null }),
      );
    } else update({ models: [], providers: [], defaultModel: null });
    refreshReadiness();
    const last = readStorage("last-session");
    await Promise.all([
      sessions,
      last && !state.active && caps.features?.session_resources
        ? openSession(last)
        : undefined,
    ]);
  } catch (error) {
    update({ connecting: false, connected: false });
    fail(error);
  }
}
export async function refreshModels(force = false) {
  update(modelInventory(await api(force ? "/models?refresh=1" : "/models")));
}
let readinessPending;
export function refreshReadiness() {
  if (readinessPending) return readinessPending;
  readinessPending = api("/readiness")
    .then((readiness) => update({ readiness }))
    .catch((e) =>
      update({
        readiness: { status: "unavailable", issues: [], message: e.message },
      }),
    )
    .finally(() => {
      readinessPending = null;
    });
  return readinessPending;
}
export async function refreshAgentInfo() {
  const info = await api("/agent");
  update({
    agentInfo: info,
    caps: { ...state.caps, talaria_extensions: info.extended_access || {} },
    readiness: info.readiness || state.readiness,
    agent: { name: info.name, name_source: info.name_source },
  });
}
export function chooseModel(model, id = state.active) {
  if (id)
    update({ modelChoices: saveModelChoice(state.modelChoices, id, model) });
  else update({ draftModel: model });
}
export function chooseReasoning(value, id = state.active) {
  if (id)
    update({
      reasoningChoices: saveReasoningChoice(state.reasoningChoices, id, value),
    });
  else update({ draftReasoning: value });
}
let detailsRequest = 0;
export async function refreshSessionDetails(id = state.active) {
  if (!id) return;
  const generation = navigation;
  const request = id === state.active ? ++detailsRequest : detailsRequest;
  const result = await api(`/sessions/${encodeURIComponent(id)}`);
  const session = result?.session || result;
  if (!session || typeof session !== "object" || Array.isArray(session)) return;
  if (
    state.active === id &&
    generation === navigation &&
    request === detailsRequest
  ) {
    const parent =
      session.source === "subagent" &&
      typeof session.parent_session_id === "string"
        ? { id: session.parent_session_id, title: "Parent conversation" }
        : state.readOnlyParent;
    update({ sessionDetails: session, readOnlyParent: parent });
    if (parent) writeStorage("child-view", JSON.stringify({ id, parent }));
  }
  return session;
}
export async function pinSession(session) {
  await api(`/sessions/${encodeURIComponent(session.id)}`, {
    method: "PATCH",
    body: { pinned: !session.pinned },
  });
  await refreshSessions();
  if (state.active === session.id)
    await refreshSessionDetails().catch(() => {});
  toast(session.pinned ? "Conversation unpinned" : "Conversation pinned");
}
let sessionsRequest = 0;
let sessionsPending;
export function refreshSessions(more = false) {
  if (more && sessionsPending)
    return sessionsPending.more
      ? sessionsPending.promise
      : sessionsPending.promise.then(() => {
          if (state.hasMore) return refreshSessions(true);
        });
  const generation = ++sessionsRequest;
  const offset = more ? state.sessionsOffset : 0;
  const promise = api(`/sessions?offset=${offset}`)
    .then((result) => {
      if (generation !== sessionsRequest) return;
      const incoming = result.data || result.sessions || [];
      const rows = more ? [...state.sessions, ...incoming] : incoming;
      const unique = [
        ...new Map(
          rows.map((s) => [
            s.id || s.session_id,
            { ...s, id: s.id || s.session_id },
          ]),
        ).values(),
      ];
      update({
        sessions: unique,
        hasMore: !!result.has_more,
        sessionsOffset: offset + (result.limit || 100),
      });
    })
    .finally(() => {
      if (sessionsPending?.generation === generation) sessionsPending = null;
    });
  sessionsPending = { generation, more, promise };
  return promise;
}
let navigation = 0;
let historyRequest = 0;
let historyCommitted = 0;
let olderPending;
let opening;
export const navigationVersion = () => navigation;
export async function openSession(id, parent = null) {
  try {
    const saved = JSON.parse(readStorage("child-view", "null"));
    if (!parent && saved?.id === id) parent = saved.parent;
  } catch {
    /* An invalid presentation hint does not affect the conversation. */
  }
  writeStorage("child-view", JSON.stringify(parent ? { id, parent } : null));
  const generation = ++navigation;
  const request = ++historyRequest;
  opening?.abort();
  const controller = (opening = new AbortController());
  update({
    active: id,
    history: [],
    loading: true,
    sidebar: false,
    error: "",
    sessionDetails: null,
    historyHasMore: false,
    historyOffset: 0,
    readOnlyParent: parent,
    findOpen: false,
  });
  writeStorage("last-session", id);
  try {
    const result = await api(`/sessions/${encodeURIComponent(id)}/messages`, {
      signal: controller.signal,
    });
    const history = await withCachedImages(
      result.session_id || id,
      result.data || [],
    );
    if (generation === navigation && request >= historyCommitted) {
      historyCommitted = request;
      const canonical = result.session_id || id;
      if (canonical !== id) {
        if (id in state.modelChoices)
          chooseModel(state.modelChoices[id], canonical);
        if (id in state.reasoningChoices)
          chooseReasoning(state.reasoningChoices[id], canonical);
        writeStorage("last-session", canonical);
        if (parent)
          writeStorage("child-view", JSON.stringify({ id: canonical, parent }));
      }
      update({
        active: canonical,
        history,
        loading: false,
        historyHasMore: !!result.has_more,
        historyOffset: result.next_offset ?? (result.data || []).length,
      });
      refreshSessionDetails(canonical).catch(() => {});
    }
  } catch (error) {
    if (generation === navigation && request >= historyCommitted) {
      update({ loading: false });
      if (error.name !== "AbortError") fail(error);
    }
  } finally {
    if (opening === controller) opening = null;
  }
}
export async function refreshHistory(id, commit = true, additionalState = null) {
  const generation = navigation;
  const request =
    commit && state.active === id ? ++historyRequest : historyRequest;
  const result = await api(`/sessions/${encodeURIComponent(id)}/messages`);
  const history = await withCachedImages(
    result.session_id || id,
    result.data || [],
  );
  if (
    commit &&
    state.active === id &&
    generation === navigation &&
    request >= historyCommitted &&
    (typeof commit !== "function" || commit())
  ) {
    historyCommitted = request;
    update({
      history,
      loading: false,
      historyHasMore: !!result.has_more,
      historyOffset: result.next_offset ?? (result.data || []).length,
      // Retire a saved live reply in the same snapshot as its history appears.
      ...additionalState?.(history),
    });
  }
  return history;
}
export function loadOlderMessages() {
  const id = state.active;
  if (!id || state.loading || !state.historyHasMore) return Promise.resolve();
  const generation = navigation;
  const request = historyRequest;
  const offset = state.historyOffset;
  if (
    olderPending?.generation === generation &&
    olderPending.request === request &&
    olderPending.offset === offset
  )
    return olderPending.promise;
  const pending = { generation, request, offset };
  pending.promise = (async () => {
    const result = await api(
      `/sessions/${encodeURIComponent(id)}/messages?offset=${offset}`,
    );
    const history = await withCachedImages(
      result.session_id || id,
      result.data || [],
    );
    if (
      state.active === id &&
      generation === navigation &&
      request === historyRequest &&
      offset === state.historyOffset
    ) {
      const existing = new Set(state.history.map((message) => message.id));
      update({
        history: [
          ...history.filter(
            (message) => message.id == null || !existing.has(message.id),
          ),
          ...state.history,
        ],
        historyHasMore: !!result.has_more,
        historyOffset: result.next_offset ?? offset + history.length,
      });
    }
  })().finally(() => {
    if (olderPending === pending) olderPending = null;
  });
  olderPending = pending;
  return pending.promise;
}
export function newConversation() {
  navigation++;
  historyRequest++;
  opening?.abort();
  opening = null;
  update({
    active: null,
    history: [],
    loading: false,
    historyHasMore: false,
    historyOffset: 0,
    sidebar: false,
    error: "",
    draftModel: null,
    draftReasoning: "auto",
    sessionDetails: null,
    readOnlyParent: null,
    findOpen: false,
  });
  writeStorage("last-session", "");
  writeStorage("child-view", "null");
}
export function supports(feature) {
  return state.caps.features?.[feature] === true;
}
