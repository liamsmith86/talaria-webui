import { html, useMemo } from "./lib.js";
import { ToolCode } from "./tool-code.js";
import { t } from "./i18n.js";

const PREVIEW_LIMIT = 20000;

function resultText(text) {
  // Do not parse large results merely to produce a bounded preview.
  if (text.length > PREVIEW_LIMIT) return { body: text, facts: [] };
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object") return { body: text, facts: [] };
    const key = Array.isArray(value) ? null
      : ["output", "content", "text"].find((field) => typeof value[field] === "string");
    if (!key) return { body: JSON.stringify(value, null, 2), facts: [] };
    // Show the payload as written. Error, exit-code and other sibling fields
    // are part of the result, even when output is empty or successful.
    const facts = Object.entries(value)
      .filter(([field, item]) => field !== key && item !== null && item !== "")
      .map(([field, item]) => [field, typeof item === "string" ? item : JSON.stringify(item)]);
    return { body: value[key], facts };
  } catch {
    return { body: text, facts: [] };
  }
}

function download(text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "tool-result.txt";
  document.body.append(link);
  link.click();
  link.remove();
  // Allow the browser to consume the URL before releasing its retained bytes.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ToolResult({ text, enabled }) {
  const { body, facts } = useMemo(() => resultText(typeof text === "string" ? text : ""), [text]);
  const shortened = body.length > PREVIEW_LIMIT;
  // Do not split an astral character at the preview boundary.
  const end = shortened && /[\uD800-\uDBFF]/.test(body[PREVIEW_LIMIT - 1])
    ? PREVIEW_LIMIT - 1 : PREVIEW_LIMIT;
  return html`<${ToolCode} text=${body.slice(0, end) || t("No output")}
    label=${t("Tool result")} enabled=${enabled} />
    ${facts.length > 0 && html`<dl class="tool-facts">
      ${facts.map(([field, value]) => html`<div><dt>${field}</dt><dd>${value}</dd></div>`)}
    </dl>`}
    ${shortened && html`<div class="tool-result-more">
      <small>${t("Preview shortened")}</small>
      <button class="text-button" onClick=${() => download(text)}>${t("Download full result")}</button>
    </div>`}`;
}
