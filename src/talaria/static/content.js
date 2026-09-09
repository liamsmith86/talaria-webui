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
  numberValue(v) === null ? "—" : v.toLocaleString();
export function money(v) {
  return numberValue(v) === null
    ? "—"
    : new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: v < 0.01 && v > 0 ? 6 : 4,
      }).format(v);
}
export function duration(v) {
  if (numberValue(v) === null) return "";
  return v < 60
    ? `${Number(v.toFixed(1))}s`
    : `${Math.floor(Math.round(v) / 60)}m ${Math.round(v) % 60}s`;
}
export function sourceLabel(value) {
  if (typeof value !== "string" || !value) return "";
  const names = {
    api_server: "API",
    cli: "CLI",
    tui: "Terminal",
    desktop: "Desktop",
    subagent: "Subagent",
    cron: "Scheduled",
    webui: "Web UI",
    telegram: "Telegram",
    discord: "Discord",
    slack: "Slack",
    whatsapp: "WhatsApp",
  };
  return Object.hasOwn(names, value)
    ? names[value]
    : value
        .replace(/[_-]/g, " ")
        .slice(0, 40)
        .replace(/^./, (s) => s.toUpperCase());
}
