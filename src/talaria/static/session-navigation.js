import { readStorage, writeStorage } from "./lib.js";

export function selectedSession() {
  const params = new URL(location.href).searchParams;
  return params.has("session") ? params.get("session") : readStorage("last-session");
}

export function selectedMessage() {
  const value = new URL(location.href).searchParams.get("message");
  return /^[1-9]\d{0,18}$/.test(value || "") ? value : null;
}

export function sessionURL(id, messageId = null) {
  const url = new URL(location.href);
  // An explicit empty selection keeps a new-session tab independent too.
  url.searchParams.set("session", id || "");
  if (messageId) url.searchParams.set("message", messageId);
  else url.searchParams.delete("message");
  return url.pathname + url.search + url.hash;
}

export function rememberSession(id, replace = false, messageId = null) {
  const url = sessionURL(id, messageId);
  if (url !== location.pathname + location.search + location.hash)
    history[replace ? "replaceState" : "pushState"](null, "", url);
  writeStorage("last-session", id || "");
}
