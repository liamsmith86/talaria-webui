// Derive the installation path from this module, not the selected profile or
// an untrusted forwarded header. Works with nested reverse-proxy prefixes.
const versioned = /\/static\/build-[a-f0-9]{16}\/paths\.js$/.test(new URL(import.meta.url).pathname);
export const basePath = new URL(versioned ? "../../" : "../", import.meta.url).pathname;
export const siteURL = (path = "") => basePath + path.replace(/^\/+/, "");
