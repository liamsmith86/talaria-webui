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
// Hermes stores its own events (subagent results, finished background
// processes, resumed turns, model changes) as user-role rows of these kinds.
export const NOTICE_KINDS = ["async_delegation_complete", "process_complete", "auto_continue", "model_switch"];
export const isNotice = (row) => row?.role === "user" && NOTICE_KINDS.includes(row.display_kind);
// Guidance sent while Hermes works is delivered inside that turn as a user row,
// wrapped in a marker the model trusts. It is part of the reply, not a new turn.
export const isGuidance = (row) => row?.role === "user" && row.display_kind === "steer";
// Hermes keeps a turn going by adding its own user-role rows, after a lost connection, an
// unusable response or at its turn limit. Nothing but their text marks them, as saved rows
// lose the tag.
const recoveryPrompts = [
  "[System: The previous response was cut off by a network error",
  "[System: Your previous response was truncated by the output length limit",
  "[System: Your previous tool call ",
  "[System: Your previous response contained only internal reasoning",
  "[System: Continue now. Execute the required tool calls",
  "[System: Your previous message ended the turn with a fragment",
  "Your previous turn indicated a tool call but none was included.",
  "You just executed tool calls but returned an empty response.",
  "You've reached the maximum number of tool-calling iterations allowed.",
];
export const isRecoveryPrompt = (row) => row?.role === "user" &&
  recoveryPrompts.some((prompt) => plainContent(row.content).startsWith(prompt));
export const userMessage = (row) => row?.role === "user" && !isNotice(row) && !isGuidance(row) &&
  !isRecoveryPrompt(row);
const guidanceMarker =
  /^\s*\[OUT-OF-BAND USER MESSAGE[^\]\n]*\]\s*([\s\S]*?)\s*\[\/OUT-OF-BAND USER MESSAGE\]\s*$/;
export const guidanceText = (content) => {
  const text = plainContent(content);
  return text.match(guidanceMarker)?.[1] ?? text.trim();
};
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
    oneshot: "CLI",
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
