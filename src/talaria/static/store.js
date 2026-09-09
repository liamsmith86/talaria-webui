import { useEffect, useState, readStorage, writeStorage } from "./lib.js";
import { api, setCSRF } from "./api.js";
import { modelInventory, readModelChoices, saveModelChoice } from "./models.js";

const listeners = new Set();
export const state = {
  auth: null,
  connected: false,
  connecting: false,
  sessions: [],
  hasMore: false,
  active: null,
  history: [],
  historyHasMore: false,
  loading: false,
  lives: {},
  caps: {},
  models: [],
  providers: [],
  defaultModel: null,
  modelChoices: readModelChoices(),
  draftModel: null,
  agent: { name: "Hermes", name_source: "fallback" },
  agentInfo: null,
  version: "",
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
  else
    update({
      error: error.message || "Something went wrong. Please try again.",
    });
}
export async function initialize() {
  try {
    const data = await api("/bootstrap");
    setCSRF(data.csrf || "");
    update({
      auth: data.authenticated,
      agent: data.agent || state.agent,
      version: data.version,
    });
    if (data.authenticated) await connect(data.connected);
  } catch (error) {
    fail(error);
    update({ auth: false });
  }
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
    const last = readStorage("last-session");
    if (last && !state.active && caps.features?.session_resources)
      await openSession(last);
  } catch (error) {
    update({ connecting: false, connected: false });
    fail(error);
  }
}
export async function refreshModels() {
  update(modelInventory(await api("/models")));
}
export async function refreshAgentInfo() {
  const info = await api("/agent");
  update({
    agentInfo: info,
    agent: { name: info.name, name_source: info.name_source },
  });
}
export function chooseModel(model, id = state.active) {
  if (id)
    update({ modelChoices: saveModelChoice(state.modelChoices, id, model) });
  else update({ draftModel: model });
}
export async function refreshSessions(more = false) {
  const offset = more ? state.sessions.length : 0;
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
  update({ sessions: unique, hasMore: !!result.has_more });
}
let navigation = 0;
export const navigationVersion = () => navigation;
export async function openSession(id) {
  const generation = ++navigation;
  update({ active: id, history: [], loading: true, sidebar: false, error: "" });
  writeStorage("last-session", id);
  try {
    const result = await api(`/sessions/${encodeURIComponent(id)}/messages`);
    if (generation === navigation)
      update({
        history: result.data || [],
        loading: false,
        historyHasMore: (result.data || []).length >= 100,
      });
  } catch (error) {
    if (generation === navigation) {
      update({ loading: false });
      fail(error);
    }
  }
}
export async function refreshHistory(id) {
  const result = await api(`/sessions/${encodeURIComponent(id)}/messages`);
  if (state.active === id)
    update({
      history: result.data || [],
      historyHasMore: (result.data || []).length >= 100,
    });
  return result.data || [];
}
export async function loadOlderMessages() {
  const id = state.active;
  const result = await api(
    `/sessions/${encodeURIComponent(id)}/messages?offset=${state.history.length}`,
  );
  if (state.active === id)
    update({
      history: [...(result.data || []), ...state.history],
      historyHasMore: (result.data || []).length >= 100,
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
  });
  writeStorage("last-session", "");
}
export function supports(feature) {
  return state.caps.features?.[feature] === true;
}
