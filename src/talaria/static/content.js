import { formatNumber, msg, t } from "./i18n.js";

const statusNames = {
  connected: msg("Connected"), disconnected: msg("Disconnected"), connecting: msg("Connecting"),
  running: msg("Running"), stopped: msg("Stopped"), starting: msg("Starting"), stopping: msg("Stopping"),
  enabled: msg("Enabled"), disabled: msg("Disabled"), ready: msg("Ready"),
  degraded: msg("Degraded"), unavailable: msg("Unavailable"), unknown: msg("Unknown"),
  failed: msg("Failed"), error: msg("Error"), ok: msg("Healthy"),
};
export function statusLabel(value = "") {
  return Object.hasOwn(statusNames, value) ? t(statusNames[value]) :
    value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function plainContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((p) => (typeof p?.text === "string" ? p.text : ""))
    .filter(Boolean)
    .join("\n");
}
export const numberValue = (v) =>
  typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;
export const count = (v) =>
  numberValue(v) === null ? "—" : formatNumber(v);
export function money(v) {
  return numberValue(v) === null
    ? "—"
    : formatNumber(v, {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: v < 0.01 && v > 0 ? 6 : 4,
      });
}
export function duration(v) {
  if (numberValue(v) === null) return "";
  return v < 60
    ? t("{seconds}s", { seconds: formatNumber(v, { maximumFractionDigits: 1 }) })
    : t("{minutes}m {seconds}s", {
        minutes: formatNumber(Math.floor(Math.round(v) / 60)),
        seconds: formatNumber(Math.round(v) % 60),
      });
}
export function sourceLabel(value) {
  if (typeof value !== "string" || !value) return "";
  const names = {
    api_server: "API",
    cli: "CLI",
    tui: msg("Terminal"),
    desktop: msg("Desktop"),
    subagent: msg("Subagent"),
    cron: msg("Scheduled"),
    webui: msg("Web UI"),
    telegram: "Telegram",
    discord: "Discord",
    slack: "Slack",
    whatsapp: "WhatsApp",
  };
  return Object.hasOwn(names, value)
    ? t(names[value])
    : value
        .replace(/[_-]/g, " ")
        .slice(0, 40)
        .replace(/^./, (s) => s.toUpperCase());
}
