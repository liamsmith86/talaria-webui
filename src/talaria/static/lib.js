import { h, render } from "./vendor/preact.js";
import htm from "./vendor/htm.js";
import { useEffect, useState } from "./vendor/hooks.js";
import { storagePrefix } from "./profile-context.js";
export { useEffect, useState };
export { useRef, useMemo, useLayoutEffect } from "./vendor/hooks.js";
export const html = htm.bind(h);
export { render };

export function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => matchMedia(query).matches);
  useEffect(() => {
    const media = matchMedia(query);
    const update = () => setMatches(media.matches);
    media.addEventListener("change", update);
    update();
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

export function readStorage(key, fallback = "") {
  try {
    const prefix = ["theme", "palette"].includes(key)
      ? "talaria."
      : storagePrefix;
    return localStorage.getItem(`${prefix}${key}`) ?? fallback;
  } catch {
    return fallback;
  }
}
export function writeStorage(key, value) {
  try {
    const prefix = ["theme", "palette"].includes(key)
      ? "talaria."
      : storagePrefix;
    localStorage.setItem(`${prefix}${key}`, value);
  } catch {
    /* Storage is optional. */
  }
}
const timeFormat = new Intl.DateTimeFormat([], {
  hour: "numeric",
  minute: "2-digit",
});
export const humanTime = (stamp) => {
  const date = new Date(typeof stamp === "number" ? stamp * 1000 : stamp);
  return Number.isNaN(date.getTime()) ? "" : timeFormat.format(date);
};

const paths = {
  info: "M12 11v6m0-10v.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  context: "M21 12a9 9 0 1 1-9-9v9zM16 3.9A9 9 0 0 1 20.1 8",
  pin: "m16 3 5 5-4 2-3 5v3l-8-8h3l5-3zM9 15l-6 6",
  chart: "M4 3v18h17M8 16v-4m5 4V7m5 9v-6",
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  refresh: "M20 7a8 8 0 1 0 1 8M20 3v5h-5",
  back: "M20 12H4m6-6-6 6 6 6",
  image: "M3 3h18v18H3zM3 16l5-5 4 4 3-3 6 6M16 7h.01",
  plus: "M12 5v14M5 12h14",
  search: "m21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  sidebar:
    "M8 3v18M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2",
  arrow: "M12 19V5m-6 6 6-6 6 6",
  chevron: "m8 10 4 4 4-4",
  close: "m6 6 12 12M6 18 18 6",
  check: "m5 12 4 4L19 6",
  stop: "M6 6h12v12H6z",
  message: "M21 11a9 9 0 0 1-9 9H3l1.5-4.5A9 9 0 1 1 21 11",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  copy: "M8 8h12v12H8zM16 8V4H4v12h4",
  branch:
    "M6 3v12a4 4 0 0 0 4 4h2M6 8h8a4 4 0 0 0 4-4V3m-3 13 3 3-3 3M3 6l3-3 3 3",
  edit: "m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15z",
  trash: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7",
  terminal: "m5 6 5 6-5 6m8 0h6",
  spark: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z",
  globe:
    "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3c-5 5-5 13 0 18M12 3c5 5 5 13 0 18",
  file: "M14 2H5v20h14V7l-5-5v5h5M8 12h8M8 16h8",
  link: "m10 13 4-4m-5 8-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m0-2 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2",
  moon: "M21 13a9 9 0 0 1-10-10A9 9 0 1 0 21 13",
  monitor: "M3 3h18v14H3zM8 21h8m-4-4v4",
  down: "M12 5v14m-6-6 6 6 6-6",
  logout: "M9 3H3v18h6m5-5 4-4-4-4M8 12h13",
  alert: "M12 3 2 21h20zM12 9v5m0 3v.5",
};
export function Icon({ name, size = 20, ...props }) {
  return html`<svg
    width=${size}
    height=${size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width=${name === "more" ? 3 : 1.65}
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    ...${props}
  >
    <path d=${paths[name] || paths.spark} />
  </svg>`;
}
export function Mark({ size = 32 }) {
  return html`<svg
    class="brand-mark"
    width=${size}
    height=${size}
    viewBox="0 0 40 40"
    fill="none"
    aria-hidden="true"
  >
    <path
      d="M7 11h27L20 28H7l9-11H7m7 0h14M7 33h9"
      stroke="currentColor"
      stroke-width="2.4"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
  </svg>`;
}
export function IconButton({ name, label, onClick, ...props }) {
  return html`<button
    class="icon-button"
    type="button"
    title=${label}
    aria-label=${label}
    onClick=${onClick}
    ...${props}
  >
    <${Icon} name=${name} />
  </button>`;
}
