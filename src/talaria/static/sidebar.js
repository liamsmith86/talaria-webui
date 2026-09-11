import {
  html,
  useState,
  useRef,
  useLayoutEffect,
  useMemo,
  useMediaQuery,
  Icon,
  IconButton,
  Mark,
} from "./lib.js";
import {
  update,
  newConversation,
  openSession,
  refreshSessions,
  fail,
} from "./store.js";
import { running } from "./runs.js";
import { sourceLabel } from "./content.js";
import { readinessLabel } from "./readiness.js";
import { ProfileSwitch } from "./profiles.js";

function groupName(session, day) {
  const now = new Date(day);
  now.setHours(0, 0, 0, 0);
  const raw =
    session.last_active ||
    session.updated_at ||
    session.started_at ||
    session.created_at;
  const date = new Date(typeof raw === "number" ? raw * 1000 : raw);
  date.setHours(0, 0, 0, 0);
  const days = Math.round((now - date) / 86400000);
  return days <= 0
    ? "Today"
    : days === 1
      ? "Yesterday"
      : days < 7
        ? "Previous 7 days"
        : "Earlier";
}

export function Sidebar({ app }) {
  const mobile = useMediaQuery("(max-width: 700px)");
  const drawer = useRef();
  useLayoutEffect(() => {
    if (!mobile || !app.sidebar) return;
    const previous = document.querySelector('[aria-label="Open sidebar"]');
    drawer.current?.querySelector("button")?.focus();
    return () => {
      if (!document.querySelector("dialog[open]")) previous?.focus();
    };
  }, [mobile, app.sidebar]);
  const [query, setQuery] = useState("");
  const [moreBusy, setMoreBusy] = useState(false);
  const sessions = useMemo(() => {
    const search = query.toLowerCase();
    return app.sessions
      .filter((s) =>
        `${s.title || ""} ${s.model || ""}`.toLowerCase().includes(search),
      )
      .sort((a, b) => Number(b.pinned === true) - Number(a.pinned === true));
  }, [app.sessions, query]);
  const liveKey = Object.keys(app.lives)
    .filter((id) => running(id, app.lives))
    .sort()
    .join("\n");
  const day = new Date().toDateString();
  // Text deltas do not change the sidebar. Retain row VNodes until the list,
  // selection, date grouping, or running indicators actually change.
  const rows = useMemo(() => {
    const runningIds = new Set(liveKey.split("\n"));
    let lastGroup = "";
    return sessions.map((session) => {
      const group = session.pinned ? "Pinned" : query ? "" : groupName(session, day);
      const heading = group && group !== lastGroup;
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
              aria-label=${session.title || "Untitled session"}
              aria-description=${
                sourceLabel(session.source)
                  ? `Source: ${sourceLabel(session.source)}`
                  : undefined
              }
            >
              ${
                session.pinned
                  ? html`<${Icon}
                    name="pin"
                    size=${13}
                    class=${`pin-icon ${runningIds.has(session.id) ? "live" : ""}`}
                  />`
                  : html`<span
                    class=${`session-dot ${runningIds.has(session.id) ? "live" : ""}`}
                  ></span>`
              }<span class="session-caption"
                >${session.title || "Untitled session"}</span
              >${
                sourceLabel(session.source) &&
                html`<small class="source-badge" aria-hidden="true"
                >${sourceLabel(session.source)}</small
              >`
              }
            </button>
            <button
              class="session-more"
              title="Session options"
              aria-label=${`Options for ${session.title || "session"}`}
              onClick=${() =>
                update({ modal: { type: "session-menu", session } })}
            >
              <${Icon} name="more" size=${18} />
            </button>
          </div>
        </div>`;
    });
  }, [sessions, query, app.active, liveKey, day]);
  return html`<aside
    ref=${drawer}
    class=${`sidebar ${app.sidebar ? "open" : ""}`}
    aria-label="Sessions"
    role=${mobile && app.sidebar ? "dialog" : undefined}
    aria-modal=${mobile && app.sidebar ? "true" : undefined}
    inert=${mobile && !app.sidebar}
    onKeyDown=${(e) => {
      if (!mobile || !app.sidebar || e.key !== "Tab") return;
      const controls = [
        ...drawer.current.querySelectorAll(
          "button:not(:disabled), input:not(:disabled), a[href]",
        ),
      ];
      const target = e.shiftKey ? controls.at(-1) : controls[0];
      const edge = e.shiftKey ? controls[0] : controls.at(-1);
      if (document.activeElement === edge) {
        e.preventDefault();
        target?.focus();
      }
    }}
  >
    <div class="sidebar-brand">
      <button
        class="brand"
        onClick=${newConversation}
        aria-label="Talaria home"
      >
        <${Mark} size=${30} /><span>Talaria</span> ${
          app.environment === "development" &&
          html`<small class="environment-badge">Dev</small>`
        }</button
      ><${IconButton}
        name="sidebar"
        label="Close sidebar"
        class="icon-button mobile-only"
        onClick=${() => update({ sidebar: false })}
      />
    </div>
    <${ProfileSwitch} app=${app} />
    <button class="new-chat" onClick=${newConversation}>
      <${Icon} name="plus" size=${18} /><span>New session</span
      ><kbd>⌘ N</kbd>
    </button>
    <div class="search-field sidebar-search">
      <${Icon} name="search" size=${17} /><input
        aria-label="Search sessions"
        placeholder="Search sessions"
        value=${query}
        onInput=${(e) => setQuery(e.target.value)}
      /><kbd>⌘ K</kbd>
    </div>
    <nav class="session-list" aria-label="Session history">
      ${rows}
      ${
        !sessions.length &&
        html`<div class="sidebar-empty">
        ${
          query
            ? "No sessions found."
            : "No sessions yet."
        }
      </div>`
      }
      ${
        app.hasMore &&
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
        ${moreBusy ? "Loading…" : "Load more sessions"}
      </button>`
      }
    </nav>
    <div class="sidebar-footer">
      <button
        class="profile-button"
        onClick=${() => update({ modal: "settings" })}
      >
        <span class="avatar"><${Icon} name="spark" size=${17} /></span
        ><span
          >Settings<small
            ><span
              class=${`connection-dot ${app.connected && ["ok", "ready", "unknown"].includes(app.readiness.status) ? "connected" : ""}`}
            ></span
            >${readinessLabel(app)}</small
          ></span
        ><${Icon} name="settings" size=${18} />
      </button>
    </div>
  </aside>`;
}
