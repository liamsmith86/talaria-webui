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
    if (caps.features?.session_resources) await refreshSessions();
    else update({ sessions: [], hasMore: false, history: [] });
    if (caps.features?.model_options) {
      refreshModels().catch(() =>
        update({ models: [], providers: [], defaultModel: null }),
      );
    } else update({ models: [], providers: [], defaultModel: null });
    refreshReadiness();
    const last = readStorage("last-session");
    if (last && !state.active && caps.features?.session_resources)
      await openSession(last);
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
export async function refreshSessionDetails(id = state.active) {
  if (!id) return;
  const result = await api(`/sessions/${encodeURIComponent(id)}`);
  const session = result?.session || result;
  if (!session || typeof session !== "object" || Array.isArray(session)) return;
  if (state.active === id) {
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
export async function refreshSessions(more = false) {
  const offset = more ? state.sessionsOffset : 0;
  const result = await api(`/sessions?offset=${offset}`);
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
}
let navigation = 0;
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
    const result = await api(`/sessions/${encodeURIComponent(id)}/messages`);
    const history = await withCachedImages(
      result.session_id || id,
      result.data || [],
    );
    if (generation === navigation) {
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
        historyOffset: result.next_offset || (result.data || []).length,
      });
      refreshSessionDetails(canonical).catch(() => {});
    }
  } catch (error) {
    if (generation === navigation) {
      update({ loading: false });
      fail(error);
    }
  }
}
export async function refreshHistory(id, commit = true) {
  const result = await api(`/sessions/${encodeURIComponent(id)}/messages`);
  const history = await withCachedImages(
    result.session_id || id,
    result.data || [],
  );
  if (commit && state.active === id)
    update({
      history,
      historyHasMore: !!result.has_more,
      historyOffset: result.next_offset || (result.data || []).length,
    });
  return history;
}
export async function loadOlderMessages() {
  const id = state.active;
  const result = await api(
    `/sessions/${encodeURIComponent(id)}/messages?offset=${state.historyOffset}`,
  );
  const history = await withCachedImages(
    result.session_id || id,
    result.data || [],
  );
  if (state.active === id)
    update({
      history: [...history, ...state.history],
      historyHasMore: !!result.has_more,
      historyOffset: result.next_offset,
    });
}
export function newConversation() {
  navigation++;
  update({
    active: null,
    history: [],
    loading: false,
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
