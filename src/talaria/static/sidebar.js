import { html, useState, Icon, IconButton, Mark } from "./lib.js";
import {
  update,
  newConversation,
  openSession,
  refreshSessions,
  fail,
} from "./store.js";
import { running } from "./runs.js";

function groupName(session) {
  const now = new Date();
  now.setHours(0, 0, 0, 0);
  const raw =
    session.last_active ||
    session.updated_at ||
    session.started_at ||
    session.created_at;
  const date = new Date(typeof raw === "number" ? raw * 1000 : raw);
  const days = Math.floor((now - date) / 86400000);
  return days < 0
    ? "Today"
    : days < 1
      ? "Yesterday"
      : days < 7
        ? "Previous 7 days"
        : "Earlier";
}

export function Sidebar({ app }) {
  const [query, setQuery] = useState("");
  const [moreBusy, setMoreBusy] = useState(false);
  const sessions = app.sessions.filter((s) =>
    `${s.title || ""} ${s.model || ""}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  let lastGroup = "";
  return html`<aside
    class=${`sidebar ${app.sidebar ? "open" : ""}`}
    aria-label="Conversations"
  >
    <div class="sidebar-brand">
      <button
        class="brand"
        onClick=${newConversation}
        aria-label="Talaria home"
      >
        <${Mark} size=${30} /><span>Talaria</span></button
      ><${IconButton}
        name="sidebar"
        label="Close sidebar"
        class="icon-button mobile-only"
        onClick=${() => update({ sidebar: false })}
      />
    </div>
    <button class="new-chat" onClick=${newConversation}>
      <${Icon} name="plus" size=${18} /><span>New conversation</span
      ><kbd>⌘ N</kbd>
    </button>
    <div class="search-field sidebar-search">
      <${Icon} name="search" size=${17} /><input
        aria-label="Search conversations"
        placeholder="Search conversations"
        value=${query}
        onInput=${(e) => setQuery(e.target.value)}
      /><kbd>⌘ K</kbd>
    </div>
    <nav class="session-list" aria-label="Conversation history">
      ${sessions.map((session) => {
        const group = query ? "" : groupName(session);
        const heading = group !== lastGroup;
        lastGroup = group;
        return html`<div key=${session.id}>
          ${heading && html`<div class="session-group">${group}</div>`}
          <div
            class=${`session-row ${app.active === session.id ? "active" : ""}`}
          >
            <button
              class="session-select"
              onClick=${() => openSession(session.id)}
              aria-current=${app.active === session.id ? "page" : undefined}
            >
              <span
                class=${`session-dot ${running(session.id) ? "live" : ""}`}
              ></span
              ><span>${session.title || "Untitled conversation"}</span>
            </button>
            <button
              class="session-more"
              title="Conversation options"
              aria-label=${`Options for ${session.title || "conversation"}`}
              onClick=${() =>
                update({ modal: { type: "session-menu", session } })}
            >
              <${Icon} name="more" size=${18} />
            </button>
          </div>
        </div>`;
      })}
      ${!sessions.length &&
      html`<div class="sidebar-empty">
        ${query
          ? "No conversations found."
          : "A fresh start. Your conversations will find a home here."}
      </div>`}
      ${app.hasMore &&
      html`<button
        class="load-more"
        disabled=${moreBusy}
        onClick=${async () => {
          setMoreBusy(true);
          try {
            await refreshSessions(true);
          } catch (e) {
            fail(e);
          } finally {
            setMoreBusy(false);
          }
        }}
      >
        ${moreBusy ? "Loading…" : "Load more conversations"}
      </button>`}
    </nav>
    <div class="sidebar-footer">
      <button
        class="profile-button"
        onClick=${() => update({ modal: "settings" })}
      >
        <span class="avatar"><${Icon} name="spark" size=${17} /></span
        ><span
          >Your space<small
            ><span
              class=${`connection-dot ${app.connected ? "connected" : ""}`}
            ></span
            >${app.connected
              ? "Connected to Hermes"
              : "Connection needed"}</small
          ></span
        ><${Icon} name="settings" size=${18} />
      </button>
    </div>
  </aside>`;
}
