// Derive the installation path from this module, not the selected profile or
// an untrusted forwarded header. Works with nested reverse-proxy prefixes.
export const basePath = new URL("../", import.meta.url).pathname;
export const siteURL = (path = "") => basePath + path.replace(/^\/+/, "");
