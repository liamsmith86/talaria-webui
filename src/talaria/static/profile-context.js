// A profile is fixed for this page's lifetime, including background requests.
const requested =
  new URL(location.href).searchParams.get("profile") || "default";
export const activeProfile = /^(default|[a-f0-9]{32})$/.test(requested)
  ? requested
  : "unavailable";
export const storagePrefix =
  activeProfile === "default"
    ? "talaria."
    : `talaria.profile.${activeProfile}.`;
export const databaseName =
  activeProfile === "default"
    ? "talaria-drafts"
    : `talaria-drafts-${activeProfile}`;
export function apiURL(path) {
  const url = `/api${path}`;
  return activeProfile === "default"
    ? url
    : `${url}${url.includes("?") ? "&" : "?"}talaria_profile=${encodeURIComponent(activeProfile)}`;
}
