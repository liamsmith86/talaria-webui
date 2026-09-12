import { useEffect, useState, readStorage, writeStorage } from "./lib.js";
import { api, setCSRF } from "./api.js";
import { migratePendingImages, withCachedImages } from "./attachments.js";
import { selectedSession, selectedMessage, rememberSession } from "./session-navigation.js";
import { historyWindow } from "./history-page.js";
import {
  modelInventory,
  readModelChoices,
  saveModelChoice,
  readReasoningChoices,
  saveReasoningChoice,
} from "./models.js";

const listeners = new Set();
const historyListeners = new Set();
export function beforeHistoryUpdate(listener) {
  historyListeners.add(listener);
  return () => historyListeners.delete(listener);
}
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
  searchWindow: null,
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
  sidebarCollapsed: readStorage("sidebar-collapsed") === "true",
  error: "",
  toast: "",
};
let frame = 0;
export function update(changes, defer = false) {
  if (Object.hasOwn(changes, "history") && changes.history !== state.history)
    for (const listener of historyListeners) listener();
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
export function collapseSidebar(collapsed) {
  writeStorage("sidebar-collapsed", String(collapsed));
  update({ sidebarCollapsed: collapsed });
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
export async function initialize(signal = new AbortController().signal) {
  const options = () => ({ signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]) });
  try {
    const data = await api("/bootstrap", options());
    signal.throwIfAborted();
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
      error: "",
    });
    if (data.authenticated) await connect(data.connected);
  } catch (error) {
    if (error.code === "profile_missing") {
      const data = await api("/profiles", options());
      signal.throwIfAborted();
      setCSRF(data.csrf || "");
      update({
        auth: true,
        connected: false,
        profileMissing: true,
        profiles: data.profiles,
        modal: "profiles",
        error: "",
      });
      return;
    }
    // A transport failure says nothing about the login cookie. The caller
    // retains the startup/retry screen until authentication can be checked.
    throw error;
  }
}
export async function refreshProfiles() {
  const data = await api("/profiles");
  update({ profiles: data.profiles });
  return data;
}
export async function connect(configured = true) {
  // Connection edits can replace Hermes while its previous discovery is pending.
  modelsPending = null;
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
    // A slow session listing must not postpone showing the last reply.
    const sessions = caps.features?.session_resources
      ? refreshSessions()
      : Promise.resolve(update({ sessions: [], hasMore: false, history: [] }));
    if (caps.features?.model_options) {
      refreshModels().catch(() => {});
    } else update({ models: [], providers: [], defaultModel: null });
    refreshReadiness();
    const last = selectedSession();
    if (!last && !state.active) rememberSession(null, true);
    await Promise.all([
      sessions,
      last && !state.active && caps.features?.session_resources
        ? openSession(last, null, true, selectedMessage())
        : undefined,
    ]);
  } catch (error) {
    update({ connecting: false, connected: false });
    fail(error);
  }
}
let modelsPending;
export function refreshModels(force = false) {
  if (modelsPending && (!force || modelsPending.force)) return modelsPending.promise;
  const request = { force };
  modelsPending = request;
  request.promise = api(force ? "/models?refresh=1" : "/models")
    .then((data) => {
      if (modelsPending === request) update(modelInventory(data));
    })
    .catch((error) => {
      // A superseded failure must not erase a newer catalog or show a stale error.
      if (modelsPending === request) throw error;
    })
    .finally(() => {
      if (modelsPending === request) modelsPending = null;
    });
  return request.promise;
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
export async function refreshSessionDetails(id = state.active, signal) {
  if (!id) return;
  const generation = navigation;
  const request = id === state.active ? ++detailsRequest : detailsRequest;
  const result = await api(`/sessions/${encodeURIComponent(id)}`, { signal });
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
        ? { id: session.parent_session_id, title: "Parent session" }
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
  toast(session.pinned ? "Session unpinned" : "Session pinned");
}
let sessionsRequest = 0;
let sessionsPending;
export function refreshSessions(more = false, signal) {
  if (more && sessionsPending)
    return sessionsPending.more
      ? sessionsPending.promise
      : sessionsPending.promise.then(() => {
          if (state.hasMore) return refreshSessions(true);
        });
  const generation = ++sessionsRequest;
  const offset = more ? state.sessionsOffset : 0;
  const end = more ? offset + 100 : Math.max(100, state.sessionsOffset);
  const promise = (async () => {
    let next = offset, result, incoming = [];
    do {
      result = await api(`/sessions?offset=${next}`, { signal });
      if (generation !== sessionsRequest) return null;
      incoming.push(...(result.data || result.sessions || []));
      next += result.limit || 100;
    } while (result.has_more && next < end);
    return { ...result, data: incoming, next_offset: next };
  })()
    .then((result) => {
      if (!result || generation !== sessionsRequest) return;
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
        sessionsOffset: result.next_offset,
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
let navigationController = new AbortController();
export const navigationVersion = () => navigation;
export const navigationSignal = () => navigationController.signal;
function advanceNavigation() {
  navigation++;
  navigationController.abort();
  navigationController = new AbortController();
}
async function migrateCanonical(id, canonical, parent, current) {
  if (id === canonical) return true;
  await migratePendingImages(`draft.${id}`, `draft.${canonical}`);
  if (!current()) return false;
  // Compaction can rotate the transcript ID. Carry unsent text with it,
  // retaining both drafts if another tab already wrote to the continuation.
  const from = `draft.${id}`, to = `draft.${canonical}`;
  const draft = readStorage(from), existing = readStorage(to);
  if (draft) {
    const combined = existing && existing !== draft ? `${existing}\n\n${draft}` : draft;
    writeStorage(to, combined);
    if (readStorage(to) === combined) writeStorage(from, "");
  }
  if (id in state.modelChoices)
    chooseModel(state.modelChoices[id], canonical);
  if (id in state.reasoningChoices)
    chooseReasoning(state.reasoningChoices[id], canonical);
  rememberSession(canonical, true);
  if (parent)
    writeStorage("child-view", JSON.stringify({ id: canonical, parent }));
  return true;
}
async function reconcileHistoryRun(id, history, current) {
  if (!state.lives[id] || !current()) return;
  // Runs depend on the store; resolve this only when a local run needs recovery.
  const { prepareRunReconciliation } = await import("./runs.js");
  return prepareRunReconciliation(id, history, current);
}
export async function openSession(id, parent = null, replace = false, messageId = null) {
  try {
    const saved = JSON.parse(readStorage("child-view", "null"));
    if (!parent && saved?.id === id) parent = saved.parent;
  } catch {
    /* An invalid presentation hint does not affect the session. */
  }
  writeStorage("child-view", JSON.stringify(parent ? { id, parent } : null));
  advanceNavigation();
  const generation = navigation;
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
    searchWindow: messageId,
    findOpen: false,
  });
  rememberSession(id, replace, messageId);
  try {
    const path = messageId ? `around?message_id=${encodeURIComponent(messageId)}` : "messages";
    const result = await api(`/sessions/${encodeURIComponent(id)}/${path}`, {
      signal: controller.signal,
    });
    const history = await withCachedImages(
      result.session_id || id,
      result.data || [],
    );
    if (generation === navigation && request >= historyCommitted) {
      const canonical = result.session_id || id;
      const current = () => generation === navigation && request >= historyCommitted;
      if (!await migrateCanonical(id, canonical, parent, current)) return;
      const reconcile = !messageId ? await reconcileHistoryRun(id, history, current) : null;
      if (!current()) return;
      reconcile?.();
      historyCommitted = request;
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
export async function refreshHistory(id, commit = true, signal) {
  const generation = navigation;
  // A focus refresh must not invalidate an older page the reader just requested.
  if (id === state.active && olderPending?.generation === navigation)
    await olderPending.promise;
  const previous = id === state.active && generation === navigation && !state.searchWindow ? state.history : [];
  const request =
    commit && state.active === id ? ++historyRequest : historyRequest;
  const result = await historyWindow(id, previous, signal);
  const canCommit = () => commit && !signal?.aborted && !state.searchWindow && state.active === id &&
    generation === navigation && request >= historyCommitted &&
    (typeof commit !== "function" || commit());
  const history = await withCachedImages(
    result.session_id || id,
    result.data || [],
  );
  if (canCommit()) {
    const canonical = result.canonical || id;
    if (!await migrateCanonical(id, canonical, state.readOnlyParent, canCommit) || !canCommit()) return history;
    const reconcile = await reconcileHistoryRun(id, history, canCommit);
    if (!canCommit()) return history;
    reconcile?.();
    historyCommitted = request;
    update({
      ...(canonical !== id ? { active: canonical, sessionDetails: null } : {}),
      history,
      loading: false,
      historyHasMore: !!result.has_more,
      historyOffset: result.next_offset ?? (result.data || []).length,
    });
    if (canonical !== id) refreshSessionDetails(canonical).catch(() => {});
  } else if (state.active !== id || state.searchWindow) {
    // Runs can settle while another conversation or an archived search window is open.
    const reconcile = await reconcileHistoryRun(id, history, () => commit && !signal?.aborted &&
      (state.active !== id || state.searchWindow) && (typeof commit !== "function" || commit()));
    reconcile?.();
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
    const current = () => state.active === id && generation === navigation &&
      request === historyRequest && offset === state.historyOffset;
    if (result.session_id && result.session_id !== id) {
      // Compaction rotated the transcript. The old offset belongs to a different
      // history, so reopen its current tail through the guarded draft migration.
      if (current()) await openSession(id, state.readOnlyParent, true);
      return;
    }
    const history = await withCachedImages(
      result.session_id || id,
      result.data || [],
    );
    if (current()) {
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
export function newConversation(replace = false) {
  advanceNavigation();
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
    searchWindow: null,
    findOpen: false,
  });
  rememberSession(null, replace === true);
  writeStorage("child-view", "null");
}
export function supports(feature) {
  return state.caps.features?.[feature] === true;
}
