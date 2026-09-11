import { html, useMemo, useRef } from "./lib.js";

function languageFor(text, shell) {
  if (/^\s*[[{]/.test(text)) {
    try {
      JSON.parse(text);
      return "json";
    } catch {
      // Logs and partial JSON remain literal text.
    }
  }
  return shell ? "bash" : null;
}

function tokensFor(text, language, prism) {
  // Recognize only a standalone quoted Python command. Other shell syntax
  // stays with Bash; never evaluate or unescape the displayed command.
  const python = language === "bash" && prism.languages.python &&
    /^(\s*python(?:\d+(?:\.\d+)?)?\s+-c\s+)("(?:\\[\s\S]|[^"\\])*"|'[^']*')(\s*)$/.exec(text);
  if (!python) return prism.tokenize(text, prism.languages[language]);
  const [, prefix, quoted, suffix] = python;
  return [
    ...prism.tokenize(prefix + quoted[0], prism.languages.bash),
    ...prism.tokenize(quoted.slice(1, -1), prism.languages.python),
    quoted.at(-1) + suffix,
  ];
}

function tokenNodes(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(tokenNodes);
  const classes = ["token", value.type, ...[].concat(value.alias || [])];
  return html`<span class=${classes.join(" ")}>${tokenNodes(value.content)}</span>`;
}

function tokenCount(value) {
  if (typeof value === "string") return 0;
  if (Array.isArray(value)) return value.reduce((count, item) => count + tokenCount(item), 0);
  return 1 + tokenCount(value.content);
}

export function ToolCode({ text, label, shell = false, enabled = false }) {
  const cached = useRef();
  const content = useMemo(() => {
    if (cached.current?.text === text && cached.current.shell === shell)
      return cached.current.content;
    // Bound lexer work and generated DOM. Closed cards and growing results do
    // no tokenization; keeping one cached result also makes reopening free.
    if (!enabled || text.length > 4096) return text;
    const prism = window.Prism;
    const language = languageFor(text, shell);
    if (!language || !prism?.languages[language]) return text;
    let content = text;
    try {
      const tokens = tokensFor(text, language, prism);
      if (tokenCount(tokens) <= 512) content = tokenNodes(tokens);
    } catch {
      // Optional coloring must never make a tool's source unavailable.
    }
    cached.current = { text, shell, content };
    return content;
  }, [text, shell, enabled]);
  // Render token text through Preact, preserving literal source and escaping
  // tool-provided HTML without a Markdown parse or an innerHTML insertion.
  return html`<pre tabindex="0" role="region" aria-label=${label}><code>${content}</code></pre>`;
}
