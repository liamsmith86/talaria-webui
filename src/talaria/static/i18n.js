import { useEffect, useState } from "./vendor/hooks.js";

// English source strings double as keys and a zero-download fallback. Catalogs
// contain plain text, named placeholders, and CLDR plural forms, never HTML.
export const languages = [
  { code: "en", name: "English", dir: "ltr" },
  { code: "zh-Hans", name: "简体中文", dir: "ltr" },
  { code: "es", name: "Español", dir: "ltr" },
  { code: "pt-BR", name: "Português (Brasil)", dir: "ltr" },
  { code: "fr", name: "Français", dir: "ltr" },
  { code: "de", name: "Deutsch", dir: "ltr" },
  { code: "ja", name: "日本語", dir: "ltr" },
];
const storageKey = "talaria.language";
const listeners = new Set();
const catalogs = new Map();
const formats = new Map();
const placeholder = /\{([A-Za-z][A-Za-z0-9_]*)\}/g;
let catalog = {};
let request = 0;
let initialized;
let current = { locale: "en", formatLocale: "en", preference: "auto", ready: false };

export function resolveLanguage(preference, browserLanguages = navigator.languages) {
  const candidates = preference === "auto" ? browserLanguages : [preference];
  for (const candidate of candidates || []) {
    try {
      const parsed = new Intl.Locale(candidate);
      const exact = languages.find((language) => language.code === parsed.baseName);
      if (exact) return exact.code;
      const match = languages.find((language) => {
        const supported = new Intl.Locale(language.code);
        return parsed.language === supported.language &&
          parsed.maximize().script === supported.maximize().script;
      });
      if (match) return match.code;
    } catch (_error) { /* Invalid or obsolete browser/storage language tags. */ }
  }
  return "en";
}

function preference() {
  try {
    const saved = localStorage.getItem(storageKey);
    return languages.some(({ code }) => code === saved) ? saved : "auto";
  } catch (_error) { return "auto"; }
}

function notify() {
  document.documentElement.lang = current.locale;
  document.documentElement.dir = languages.find(({ code }) => code === current.locale).dir;
  for (const listener of listeners) listener(current);
}

function numberLocale(locale, selected) {
  if (selected !== "auto") return locale;
  for (const value of navigator.languages || []) {
    try {
      if (resolveLanguage(value) === locale && Intl.NumberFormat.supportedLocalesOf(value).length)
        return new Intl.Locale(value).toString();
    } catch (_error) { /* Ignore malformed browser locale hints. */ }
  }
  return locale;
}

async function load(locale) {
  if (locale === "en") return {};
  if (!catalogs.has(locale)) {
    const pending = fetch(new URL(`./locales/${locale}.json`, import.meta.url), {
      credentials: "same-origin",
      signal: AbortSignal.timeout(2500),
    }).then(async (response) => {
      if (!response.ok) throw new Error("Language catalog unavailable");
      const data = await response.json();
      if (!data || typeof data !== "object" || Array.isArray(data))
        throw new Error("Invalid language catalog");
      const result = Object.create(null);
      for (const [source, value] of Object.entries(data)) {
        if (compatible(source, value)) result[source] = value;
        else if (value && typeof value === "object" && !Array.isArray(value)) {
          const forms = {};
          for (const category of ["zero", "one", "two", "few", "many", "other"])
            if (compatible(source, value[category])) forms[category] = value[category];
          if (forms.other) result[source] = forms;
        }
      }
      return result;
    }).catch((error) => {
      catalogs.delete(locale);
      throw error;
    });
    catalogs.set(locale, pending);
  }
  return catalogs.get(locale);
}

export async function setLanguage(value, persist = true) {
  const selected = languages.some(({ code }) => code === value) ? value : "auto";
  const locale = resolveLanguage(selected);
  const generation = ++request;
  const next = await load(locale);
  if (generation !== request) return;
  catalog = next;
  // Keep regional date/number conventions when following browser preferences.
  current = { locale, formatLocale: numberLocale(locale, selected), preference: selected, ready: true };
  formats.clear();
  if (persist) {
    try { localStorage.setItem(storageKey, selected); } catch (_error) { /* Optional. */ }
  }
  notify();
}

export function initializeLanguage() {
  if (!initialized) {
    initialized = setLanguage(preference(), false).catch(() => {}).finally(() => {
      // A missing or superseded catalog must never prevent login. A concurrent
      // preference change can fail after this request has lost its generation.
      if (!current.ready) {
        current = { ...current, ready: true };
        notify();
      }
    });
  }
  return initialized;
}

export function useLanguage() {
  const [snapshot, set] = useState(current);
  useEffect(() => {
    listeners.add(set);
    set(current);
    initializeLanguage();
    return () => listeners.delete(set);
  }, []);
  return snapshot;
}

window.addEventListener("storage", (event) => {
  if (event.key === storageKey || event.key === null)
    setLanguage(preference(), false).catch(() => {});
});
window.addEventListener("languagechange", () => {
  if (current.preference === "auto") setLanguage("auto", false).catch(() => {});
});

function compatible(source, value) {
  if (typeof value !== "string" || !value) return false;
  const names = (text) => [...new Set([...text.matchAll(placeholder)].map((m) => m[1]))].sort().join(",");
  return names(source) === names(value);
}

function message(source) {
  if (typeof source !== "string") return "";
  const value = Object.hasOwn(catalog, source) ? catalog[source] : null;
  return typeof value === "string" ? value : source;
}

export function t(source, values = {}) {
  return message(source).replace(placeholder, (match, name) =>
    Object.hasOwn(values, name) ? String(values[name]) : match);
}

export function rich(source, values) {
  // Preact escapes text and renders supplied nodes. No translated markup is
  // parsed, and translators can move an entire link or code fragment safely.
  return message(source).split(placeholder).map((part, index) =>
    index % 2 ? (Object.hasOwn(values, part) ? values[part] : `{${part}}`) : part);
}

function formatter(type, options) {
  const key = `${type}:${JSON.stringify(options)}`;
  if (!formats.has(key)) {
    if (formats.size >= 32) formats.clear();
    formats.set(key, new Intl[type](current.formatLocale, options));
  }
  return formats.get(key);
}

export const formatNumber = (value, options) => formatter("NumberFormat", options).format(value);
export const formatDate = (value, options) => formatter("DateTimeFormat", options).format(value);

// Mark source labels stored in arrays/state; translate them only when rendered.
export const msg = (source) => source;

export function n(one, other, count, values = {}) {
  const forms = Object.hasOwn(catalog, one) ? catalog[one] : null;
  const category = formatter("PluralRules").select(count);
  const translated = forms && typeof forms === "object" ? forms[category] ?? forms.other : null;
  const fallback = count === 1 ? one : other;
  const text = typeof translated === "string" ? translated : fallback;
  const parameters = { ...values, count: formatNumber(count) };
  return text.replace(placeholder, (match, name) =>
    Object.hasOwn(parameters, name) ? String(parameters[name]) : match);
}
