import { html, useMemo } from "./lib.js";
import { ToolCode } from "./tool-code.js";
import { t } from "./i18n.js";

const PREVIEW_LIMIT = 20000;

function resultText(text) {
  // Do not parse large results merely to produce a bounded preview.
  if (text.length > PREVIEW_LIMIT) return text;
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object") return text;
    const keys = Object.keys(value);
    // Unwrap only a single payload field. Error, exit-code and other sibling
    // fields are part of the result, even when output is empty or successful.
    if (keys.length === 1 && ["output", "content", "text"].includes(keys[0]) &&
      typeof value[keys[0]] === "string") return value[keys[0]];
    return JSON.stringify(value, null, 2);
  } catch {
    return text;
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
  const output = useMemo(() => resultText(typeof text === "string" ? text : ""), [text]);
  const shortened = output.length > PREVIEW_LIMIT;
  // Do not split an astral character at the preview boundary.
  const end = shortened && /[\uD800-\uDBFF]/.test(output[PREVIEW_LIMIT - 1])
    ? PREVIEW_LIMIT - 1 : PREVIEW_LIMIT;
  return html`<${ToolCode} text=${output.slice(0, end) || t("No output")}
    label=${t("Tool result")} enabled=${enabled} />
    ${shortened && html`<div class="tool-result-more">
      <small>${t("Preview shortened")}</small>
      <button class="text-button" onClick=${() => download(text)}>${t("Download full result")}</button>
    </div>`}`;
}
