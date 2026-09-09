import { html, render, useEffect, useState, useRef, Icon, IconButton, Mark, readStorage, writeStorage } from './lib.js';
import { api } from './api.js';
import { useStore, initialize, state, update, fail, newConversation, supports } from './store.js';
import { Sidebar } from './sidebar.js';
import { Conversation } from './conversation.js';
import { Composer } from './composer.js';
import { Connection, Settings, Dialog, SessionDialog, ModelPicker } from './dialogs.js';
import { restoreRuns, running } from './runs.js';

function Login() {
  const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(e) { e.preventDefault(); setBusy(true); setError('');
    try { await api('/login', { method: 'POST', body: { password } }); await initialize(); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return html`<main class="login-page"><div class="login-brand"><${Mark} size=${42}/><span>Talaria</span></div>
    <div class="login-card"><span class="eyebrow">YOUR SPACE, AWAITING YOU</span><h1>Welcome back.</h1><p>A little space for big ideas.</p>
    <form onSubmit=${submit}><label class="field">Password<input type="password" value=${password} onInput=${e => setPassword(e.target.value)} autocomplete="current-password" required autoFocus placeholder="Your Talaria password"/></label>
    ${error && html`<div class="form-error" role="alert">${error}</div>`}<button class="button primary" disabled=${busy}>${busy ? 'Opening your space…' : 'Step inside'}<${Icon} name="arrow" size=${17}/></button></form></div>
    <p class="login-footer">A considered companion for Hermes Agent.</p>
  </main>`;
}

function Welcome({ onSuggestion }) {
  const suggestions = [
    ['spark', 'Make sense of something', 'Help me think through '],
    ['terminal', 'Build something useful', 'I’d like to build '],
    ['file', 'Find the right words', 'Help me write '],
  ];
  return html`<div class="welcome"><div class="welcome-mark"><${Mark} size=${49}/></div><div class="eyebrow">A LITTLE MORE ROOM TO THINK</div><h1>Where will your<br/>curiosity take you?</h1><p>A thought, a question, the beginning of something.<br class="desktop-only"/> Your Hermes is here to help.</p>
    <div class="suggestions">${suggestions.map(([icon, label, text]) => html`<button onClick=${() => onSuggestion({ text, time: Date.now() })}><${Icon} name=${icon} size=${19}/><span>${label}</span><span class="suggestion-arrow">↗</span></button>`)}</div>
  </div>`;
}

function App() {
  const app = useStore();
  const [model, setModel] = useState(() => { try { return JSON.parse(readStorage('model', 'null')); } catch { return null; } });
  const [modelOpen, setModelOpen] = useState(false); const [suggestion, setSuggestion] = useState(null);
  const restored = useRef(false);
  useEffect(() => { initialize();
    const keydown = e => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault(); update({ sidebar: true }); setTimeout(() => document.querySelector('[aria-label="Search conversations"]')?.focus(), 10);
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n') { e.preventDefault(); newConversation(); }
      if (e.key === 'Escape') update({ sidebar: false });
    };
    window.addEventListener('keydown', keydown); return () => window.removeEventListener('keydown', keydown);
  }, []);
  useEffect(() => { if (app.connected && !restored.current) { restored.current = true; restoreRuns(); } }, [app.connected]);
  const current = app.sessions.find(s => s.id === app.active) || { id: app.active, title: 'Conversation' };
  const close = () => update({ modal: null });
  if (app.auth === null) return html`<div class="initial-loader" role="status"><${Mark} size=${40}/><span>Opening your space…</span></div>`;
  if (!app.auth) return html`<${Login}/>`;
  return html`<div class="app-shell">
    <${Sidebar} app=${app}/>${app.sidebar && html`<button class="sidebar-overlay" aria-label="Close sidebar" onClick=${() => update({ sidebar: false })}/>`}
    <main class=${`main-panel ${!app.active ? 'is-new' : ''}`}>
      <header class="topbar"><div class="topbar-left"><${IconButton} name="sidebar" label="Open sidebar" class="icon-button mobile-only" onClick=${() => update({ sidebar: true })}/><span class="topbar-title">${app.active ? current.title || 'Untitled conversation' : 'Your next beginning'}</span></div><div class="topbar-actions">${app.active ? html`<${IconButton} name="more" label="Conversation options" onClick=${() => update({ modal: { type: 'session-menu', session: current } })}/>` : html`<span class="private-label"><span class="tiny-dot"/> Yours, with Hermes</span>`}</div></header>
      ${app.error && html`<div class="error-banner" role="alert"><${Icon} name="alert" size=${17}/><span>${app.error}</span><${IconButton} name="close" label="Dismiss error" onClick=${() => update({ error: '' })}/></div>`}
      ${!app.connected && !app.connecting && html`<div class="connection-banner"><span>Connect Hermes to start a conversation.</span><button class="text-button" onClick=${() => update({ modal: 'connection' })}>Set up connection<${Icon} name="link" size=${15}/></button></div>`}
      ${app.active ? html`<${Conversation} app=${app}/>` : html`<${Welcome} onSuggestion=${setSuggestion}/>`}
      <div class="composer-area"><${Composer} app=${app} model=${model} onModel=${() => setModelOpen(true)} draftSuggestion=${suggestion}/><div class="composer-footnote"><span>${app.active ? 'A little room to think. A little more flow.' : 'Powered by your Hermes. Shaped around you.'}</span><span class="keyboard-hint">${running(app.active) ? 'Your agent keeps working if you leave.' : 'Shift + Enter for a new line'}</span></div></div>
    </main>
    ${app.toast && html`<div class="toast" role="status"><${Icon} name="check" size=${17}/>${app.toast}</div>`}
    ${app.modal === 'settings' && html`<${Settings} onClose=${close}/>`}
    ${app.modal === 'connection' && html`<${Connection} initial=${!app.connected} onClose=${close}/>`}
    ${app.modal?.type === 'session-menu' && html`<${Dialog} title=${app.modal.session.title || 'Conversation'} onClose=${close}>
      <div class="session-menu">${[['rename', 'edit', 'Rename'], ['fork', 'branch', 'Branch conversation'], ['delete', 'trash', 'Delete conversation']].map(([type, icon, label]) => html`<button class=${type === 'delete' ? 'danger-text' : ''} disabled=${(type === 'fork' && !supports('session_fork')) || (type === 'delete' && running(app.modal.session.id))} onClick=${() => update({ modal: { type, session: app.modal.session } })}><${Icon} name=${icon} size=${19}/>${label}</button>`)}</div>
    </${Dialog}>`}
    ${['rename', 'delete', 'fork'].includes(app.modal?.type) && html`<${SessionDialog} mode=${app.modal.type} session=${app.modal.session} onClose=${close}/>`}
    ${modelOpen && html`<${ModelPicker} models=${app.models} selected=${model} onSelect=${m => { setModel(m); writeStorage('model', JSON.stringify(m)); }} onClose=${() => setModelOpen(false)}/>`}
  </div>`;
}
render(html`<${App}/>`, document.getElementById('app'));
