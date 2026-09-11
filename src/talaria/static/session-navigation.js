import { readStorage, writeStorage } from "./lib.js";

export function selectedSession() {
  const params = new URL(location.href).searchParams;
  return params.has("session") ? params.get("session") : readStorage("last-session");
}

export function sessionURL(id) {
  const url = new URL(location.href);
  // An explicit empty selection keeps a new-session tab independent too.
  url.searchParams.set("session", id || "");
  return url.pathname + url.search + url.hash;
}

export function rememberSession(id, replace = false) {
  const url = sessionURL(id);
  if (url !== location.pathname + location.search + location.hash)
    history[replace ? "replaceState" : "pushState"](null, "", url);
  writeStorage("last-session", id || "");
}
