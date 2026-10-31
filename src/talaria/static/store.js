import { useEffect, useState, readStorage, writeStorage } from "./lib.js";
import { api, setCSRF } from "./api.js";
import { migratePendingImages, withCachedImages } from "./attachments.js";
import { selectedSession, selectedMessage, rememberSession } from "./session-navigation.js";
import { historyWindow } from "./history-page.js";
import { msg } from "./i18n.js";
import { importBrowserChoices, modelChoice, modelInventory, reasoningChoice } from "./models.js";

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
  // Whether Hermes answered for this history: empty then means nothing is saved,
  // where empty after a failed read means nothing is known.
  historyLoaded: false,
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
  // Each session's model and reasoning choice, as Talaria's server records it.
  modelChoices: {},
  draftModel: null,
  reasoningChoices: {},
  draftReasoning: "auto",
  // Native commands Talaria recorded for the open session and its compaction lineage.
  commands: { session: null, records: [], clock: 0 },
  // Each session's latest run as Hermes reports it to every device.
  activity: {},
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
  // Text returned to a session's message box, such as guidance Hermes never received.
  restoredDraft: null,
};
let frame = 0;
// Revalidation repeats reads that usually find nothing new. Skipping those
// updates keeps an idle page from re-rendering.
const same = (a, b) => a === b || JSON.stringify(a) === JSON.stringify(b);
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
// Text returned to a session's message box, unless someone is writing there.
export function restoreDraft(session, text) {
  const key = `draft.${session || "new"}`;
  if (readStorage(key).trim()) return false;
  writeStorage(key, text);
  update({ restoredDraft: { key, text } });
  return true;
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
  } else if (!error.shownInReply)
    update({
      error: error.message || msg("Something went wrong. Please try again."),
    });
}
export async function initialize(signal = new AbortController().signal) {
  const options = () => ({ signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]) });
  try {
    const data = await api("/bootstrap", options());
    signal.throwIfAborted();
    if (data.environment === "demo")
      await (await import("./demo.js")).enableDemo(signal, data.version, data.plugin_version);
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
  readinessPending = null;
  readinessRequest++;
  agentInfoRequest++;
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
      activity: {},
    });
    // The sidebar, current transcript, catalog, and readiness are independent.
    // A slow session listing must not postpone showing the last reply.
    const sessions = caps.features?.session_resources
      ? refreshSessions()
      : Promise.resolve(update({ sessions: [], hasMore: false, history: [], historyLoaded: false }));
    if (caps.features?.model_options) {
      refreshModels().catch(() => {});
    } else update({ models: [], providers: [], defaultModel: null });
    refreshReadiness();
    if (caps.talaria_extensions?.session_activity) refreshActivity();
    importBrowserChoices(api).then((imported) => {
      if (imported && state.active) return refreshSessionDetails(state.active);
    }).catch(() => {});
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
let readinessRequest = 0;
let agentInfoRequest = 0;
export function refreshReadiness() {
  if (readinessPending) return readinessPending;
  const request = ++readinessRequest;
  const pending = api("/readiness")
    .then((readiness) => {
      if (request === readinessRequest) update({ readiness });
    })
    .catch((e) => {
      if (request === readinessRequest) update({
        readiness: { status: "unavailable", issues: [], message: e.message },
      });
    })
    .finally(() => {
      if (readinessPending === pending) readinessPending = null;
    });
  readinessPending = pending;
  return pending;
}
export async function refreshAgentInfo() {
  const request = ++agentInfoRequest;
  // Agent details also report health; both endpoints share the latest check.
  const healthRequest = ++readinessRequest;
  try {
    const info = await api("/agent");
    if (request !== agentInfoRequest) return;
    update({
      agentInfo: info,
      caps: { ...state.caps, talaria_extensions: info.extended_access || {} },
      readiness: healthRequest === readinessRequest ? info.readiness || state.readiness : state.readiness,
      agent: { name: info.name, name_source: info.name_source },
    });
  } catch (error) {
    if (request === agentInfoRequest) throw error;
  }
}
// A choice made on one device applies on every device. It shows at once, and
// session details requested before the latest change settled never undo it.
const choiceChanges = new Map();
const choiceVersion = (id) => choiceChanges.get(id)?.version ?? 0;
function withChoice(choices, id, value, present = true) {
  const next = { ...choices };
  if (present) next[id] = value;
  else delete next[id];
  return next;
}
function applyPreferences(id, saved, version) {
  if (!saved || typeof saved !== "object" || choiceChanges.get(id)?.pending ||
      choiceVersion(id) !== version) return;
  const changes = {};
  for (const [key, field] of [["modelChoices", "model"], ["reasoningChoices", "reasoning"]]) {
    const present = Object.hasOwn(saved, field);
    if (present !== Object.hasOwn(state[key], id) || !same(state[key][id], saved[field]))
      changes[key] = withChoice(state[key], id, saved[field], present);
  }
  if (Object.keys(changes).length) update(changes);
}
async function savePreference(id, field, value) {
  const key = field === "model" ? "modelChoices" : "reasoningChoices";
  const previous = state[key], had = Object.hasOwn(previous, id);
  const changing = choiceChanges.get(id) || { pending: 0, version: 0 };
  choiceChanges.set(id, changing);
  changing.pending++;
  changing.version++;
  update({ [key]: withChoice(previous, id, value) });
  try {
    await api(`/sessions/${encodeURIComponent(id)}/preferences`, {
      method: "PUT",
      body: { [field]: value },
    });
  } catch (error) {
    if (state[key][id] === value)
      update({ [key]: withChoice(state[key], id, previous[id], had) });
    fail(error);
  } finally {
    changing.pending--;
    changing.version++;
  }
}
export function chooseModel(model, id = state.active) {
  if (id) return savePreference(id, "model", modelChoice(model));
  update({ draftModel: model });
}
export function chooseReasoning(value, id = state.active) {
  if (id) return savePreference(id, "reasoning", reasoningChoice(value));
  update({ draftReasoning: value });
}
let detailsRequest = 0;
export async function refreshSessionDetails(id = state.active, signal) {
  if (!id) return;
  const generation = navigation;
  const request = id === state.active ? ++detailsRequest : detailsRequest;
  const version = choiceVersion(id);
  let result;
  try {
    result = await api(`/sessions/${encodeURIComponent(id)}`, { signal });
  } catch (error) {
    // Deleted elsewhere: leave for a new session instead of keeping a
    // transcript and message box that can no longer reach Hermes.
    if (error.status === 404 && state.active === id && generation === navigation) {
      newConversation(true);
      toast(msg("This session was deleted."));
    }
    throw error;
  }
  const session = result?.session || result;
  if (!session || typeof session !== "object" || Array.isArray(session)) return;
  if (generation === navigation) applyPreferences(id, result.talaria_preferences, version);
  if (
    state.active === id &&
    generation === navigation &&
    request === detailsRequest
  ) {
    const parent =
      session.source === "subagent" &&
      typeof session.parent_session_id === "string"
        ? { id: session.parent_session_id, title: msg("Parent session") }
        : state.readOnlyParent;
    if (!same(session, state.sessionDetails) || !same(parent, state.readOnlyParent))
      update({ sessionDetails: session, readOnlyParent: parent });
    if (parent) writeStorage("child-view", JSON.stringify({ id, parent }));
  }
  return session;
}
let commandsRequest = 0;
// Results dismissed here until Talaria confirms it: a list read before then
// still has them.
const dismissing = new Set();
// Commands recorded for this session, including rows a compaction continuation
// inherited from its earlier sessions.
export async function refreshCommands(id = state.active, signal) {
  if (!id || state.caps.talaria_extensions?.commands !== true) return;
  const generation = navigation;
  const request = ++commandsRequest;
  const lineage = [...new Set(state.history.map((row) => row?.session_id)
    .filter((sid) => typeof sid === "string" && sid && sid !== id))];
  const query = lineage.length ? `?lineage=${encodeURIComponent(lineage.join(","))}` : "";
  const result = await api(`/sessions/${encodeURIComponent(id)}/commands${query}`, { signal });
  if (state.active !== id || generation !== navigation || request !== commandsRequest) return;
  const clock = Number(result.server_time) * 1000 - Date.now();
  const records = (Array.isArray(result.data) ? result.data : [])
    .filter((record) => !dismissing.has(record.id));
  if (state.commands.session === id && same(records, state.commands.records)) return;
  update({ commands: { session: id, records, clock: Number.isFinite(clock) ? clock : 0 } });
}
// Talaria answered a command's start before its list caught up. Show the
// record now. Refreshing supersedes any list requested before the answer.
export function showCommand(record) {
  const { commands } = state;
  if (commands.session === record.session_id && !commands.records.some((r) => r.id === record.id))
    update({ commands: { ...commands, records: [...commands.records, record] } });
  refreshCommands().catch(() => {});
}
// Dismissing a result applies on every device.
export async function dismissCommand(id) {
  dismissing.add(id);
  const { commands } = state;
  update({ commands: { ...commands, records: commands.records.filter((record) => record.id !== id) } });
  try {
    await api(`/commands/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch {
    /* The list read next shows whatever Talaria kept. */
  } finally {
    // Supersedes any list read before Talaria forgot it.
    dismissing.delete(id);
    refreshCommands().catch(() => {});
  }
}
let activityPending;
const runStatuses = new Set(["queued", "running", "waiting_for_approval", "waiting_for_input",
  "completed", "failed", "cancelled", "interrupted"]);
// Shared by the sidebar's run status and following a reply started elsewhere.
export function refreshActivity() {
  activityPending ??= api("/activity")
    .then((result) => Object.fromEntries((Array.isArray(result.data) ? result.data : [])
      .filter((run) => typeof run?.session_id === "string" && runStatuses.has(run.status))
      .map((run) => [run.session_id, run])))
    .catch(() => ({}))
    .then((activity) => {
      if (!same(activity, state.activity)) update({ activity });
    })
    .finally(() => { activityPending = null; });
  return activityPending;
}
export async function pinSession(session) {
  await api(`/sessions/${encodeURIComponent(session.id)}`, {
    method: "PATCH",
    body: { pinned: !session.pinned },
  });
  await refreshSessions();
  if (state.active === session.id)
    await refreshSessionDetails().catch(() => {});
  toast(session.pinned ? msg("Session unpinned") : msg("Session pinned"));
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
  return loadSessions(more, signal);
}
// Revalidation never discards a page someone asked for. Between focus changes
// it reads only the first page, where other devices' changes appear.
export function revalidateSessions(signal, recent = false) {
  if (sessionsPending?.more) return sessionsPending.promise;
  return loadSessions(false, signal, recent);
}
function loadSessions(more, signal, recent = false) {
  const generation = ++sessionsRequest;
  const offset = more ? state.sessionsOffset : 0;
  const end = more ? offset + 100 : recent ? 0 : Math.max(100, state.sessionsOffset);
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
      let unique = [
        ...new Map(
          rows.map((s) => [
            s.id || s.session_id,
            { ...s, id: s.id || s.session_id },
          ]),
        ).values(),
      ];
      // Pages loaded beyond the first stay as they were.
      const deeper = recent && result.has_more && state.sessionsOffset > result.next_offset;
      if (deeper) {
        const fresh = new Set(unique.map((s) => s.id));
        unique = [...unique, ...state.sessions.filter((s) => !fresh.has(s.id))];
      }
      const hasMore = deeper ? state.hasMore : !!result.has_more;
      const sessionsOffset = deeper ? state.sessionsOffset : result.next_offset;
      if (same(unique, state.sessions) && state.hasMore === hasMore &&
          state.sessionsOffset === sessionsOffset) return;
      update({ sessions: unique, hasMore, sessionsOffset });
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
  // Talaria's server carries the choice to the continuation. Carry it here
  // too, so the picker does not change while the continuation's details load.
  if (Object.hasOwn(state.modelChoices, id) && !Object.hasOwn(state.modelChoices, canonical))
    update({ modelChoices: withChoice(state.modelChoices, canonical, state.modelChoices[id]) });
  if (Object.hasOwn(state.reasoningChoices, id) && !Object.hasOwn(state.reasoningChoices, canonical))
    update({ reasoningChoices: withChoice(state.reasoningChoices, canonical, state.reasoningChoices[id]) });
  if (state.commands.session === id) update({ commands: { ...state.commands, session: canonical } });
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
    historyLoaded: false,
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
        historyLoaded: true,
        loading: false,
        historyHasMore: !!result.has_more,
        historyOffset: result.next_offset ?? (result.data || []).length,
      });
      refreshSessionDetails(canonical).catch(() => {});
    }
  } catch (error) {
    if (generation === navigation && request >= historyCommitted) {
      update({ loading: false });
      // A missing transcript may be a deleted session, which opens a new one.
      if (error.status === 404) await refreshSessionDetails(id).catch(() => {});
      if (error.name !== "AbortError" && generation === navigation) fail(error);
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
      historyLoaded: true,
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
    historyLoaded: false,
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
// Hermes 0.22.1+ takes API turns only in its API's own sessions and group rooms.
// Its session authority is also what advertises unknown-run resolution.
export function externalSource(session) {
  const source = session?.source;
  return supports("run_unknown_resolution") && typeof source === "string" &&
    !["", "api_server", "bot_room"].includes(source) ? source : "";
}
// A native fork ends the original; only the plugin copies another app's session.
export function canBranch(session) {
  return supports("session_fork") && (!session?.source || session.source === "api_server" ||
    state.caps.talaria_extensions?.external_session_copies === true);
}
