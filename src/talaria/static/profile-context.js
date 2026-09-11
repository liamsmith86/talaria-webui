import { basePath, siteURL } from "./paths.js";

// A profile is fixed for this page's lifetime, including background requests.
const requested =
  new URL(location.href).searchParams.get("profile") || "default";
export const activeProfile = /^(default|[a-f0-9]{32})$/.test(requested)
  ? requested
  : "unavailable";
const installation = basePath === "/" ? "talaria." : `talaria.mount.${encodeURIComponent(basePath)}.`;
export const storagePrefix =
  activeProfile === "default"
    ? installation
    : `${installation}profile.${activeProfile}.`;
export const databaseName =
  "talaria-drafts" +
  (activeProfile === "default" ? "" : `-${activeProfile}`) +
  (basePath === "/" ? "" : `-${encodeURIComponent(basePath)}`);
export function apiURL(path) {
  const url = siteURL(`api${path}`);
  return activeProfile === "default"
    ? url
    : `${url}${url.includes("?") ? "&" : "?"}talaria_profile=${encodeURIComponent(activeProfile)}`;
}
