import { useEffect, useState, readStorage, writeStorage } from './lib.js';
import { api, setCSRF } from './api.js';

const listeners = new Set();
export const state = {
  auth: null, connected: false, connecting: false, sessions: [], hasMore: false,
  active: null, history: [], loading: false, lives: {}, caps: {}, models: [],
  modal: null, sidebar: false, error: '', toast: '',
};
export function update(changes) { Object.assign(state, changes); for (const listener of listeners) listener({ ...state }); }
export function useStore() {
  const [snapshot, set] = useState({ ...state });
  useEffect(() => { listeners.add(set); return () => listeners.delete(set); }, []);
  return snapshot;
}
let toastTimer;
export function toast(message) {
  clearTimeout(toastTimer); update({ toast: message });
  toastTimer = setTimeout(() => update({ toast: '' }), 3500);
}
export function fail(error) {
  if (error.code === 'unauthenticated') update({ auth: false });
  else update({ error: error.message || 'Something went wrong. Please try again.' });
}
export async function initialize() {
  try {
    const data = await api('/bootstrap'); setCSRF(data.csrf || '');
    update({ auth: data.authenticated });
    if (data.authenticated) await connect(data.connected);
  } catch (error) { fail(error); update({ auth: false }); }
}
export async function connect(configured = true) {
  if (!configured) { update({ connected: false, modal: 'connection' }); return; }
  update({ connecting: true, error: '' });
  try {
    const caps = await api('/capabilities');
    update({ caps, connected: true, connecting: false });
    await refreshSessions();
    if (caps.features?.model_options) {
      api('/models').then(result => update({ models: flattenModels(result) })).catch(() => {});
    }
    const last = readStorage('last-session');
    if (last && !state.active) await openSession(last);
  } catch (error) { update({ connecting: false, connected: false }); fail(error); }
}
export function flattenModels(data) {
  const options = [];
  const providers = data.providers || data.data || [];
  for (const p of Array.isArray(providers) ? providers : Object.values(providers)) {
    const provider = p.slug || p.id || p.provider || p.name || '';
    for (const m of p.models || []) {
      const id = typeof m === 'string' ? m : m.id || m.model || m.name;
      if (id) options.push({ id, provider, label: typeof m === 'string' ? m : m.name || m.label || id,
        providerLabel: p.label || p.name || provider });
    }
  }
  return options;
}
export async function refreshSessions(more = false) {
  const offset = more ? state.sessions.length : 0;
  const result = await api(`/sessions?offset=${offset}`);
  const incoming = result.data || result.sessions || [];
  const rows = more ? [...state.sessions, ...incoming] : incoming;
  const unique = [...new Map(rows.map(s => [s.id || s.session_id, { ...s, id: s.id || s.session_id }])).values()];
  update({ sessions: unique, hasMore: !!result.has_more });
}
let navigation = 0;
export async function openSession(id) {
  const generation = ++navigation;
  update({ active: id, history: [], loading: true, sidebar: false, error: '' });
  writeStorage('last-session', id);
  try {
    const result = await api(`/sessions/${encodeURIComponent(id)}/messages`);
    if (generation === navigation) update({ history: result.data || [], loading: false });
  } catch (error) { if (generation === navigation) { update({ loading: false }); fail(error); } }
}
export async function refreshHistory(id) {
  const result = await api(`/sessions/${encodeURIComponent(id)}/messages`);
  if (state.active === id) update({ history: result.data || [] });
  return result.data || [];
}
export function newConversation() {
  navigation++;
  update({ active: null, history: [], loading: false, sidebar: false, error: '' });
  writeStorage('last-session', '');
}
export function supports(feature) { return state.caps.features?.[feature] === true; }
