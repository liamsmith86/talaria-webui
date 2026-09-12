// Extract source messages and validate catalogs using the existing ESLint parser.
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { execFileSync } from "node:child_process";
import { parse } from "espree";

const root = "src/talaria/static";
const catalogRoot = path.join(root, "locales");
const messages = new Map();
const errors = [];
const placeholders = (value) => [...new Set([...value.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)]
  .map((match) => match[1]))].sort().join(",");
const literal = (node) => node?.type === "Literal" && typeof node.value === "string" ? node.value : null;

// Server-owned diagnostics use the same exact-text fallback. Upstream/model
// output is never parsed or rewritten to fit a translation key.
for (const source of JSON.parse(execFileSync(process.env.TALARIA_I18N_PYTHON || "python3",
  ["contrib/i18n.py"], { encoding: "utf8" }))) messages.set(source, source);

function walk(node, visit) {
  if (!node || typeof node !== "object") return;
  visit(node);
  for (const value of Object.values(node))
    if (Array.isArray(value)) value.forEach((item) => walk(item, visit));
    else if (value && typeof value === "object") walk(value, visit);
}

for (const file of fs.readdirSync(root).filter((name) => name.endsWith(".js"))) {
  const ast = parse(fs.readFileSync(path.join(root, file), "utf8"), {
    ecmaVersion: "latest", sourceType: "module", loc: true,
  });
  walk(ast, (node) => {
    if (node.type !== "CallExpression" || node.callee.type !== "Identifier" ||
        !["t", "n", "rich", "msg"].includes(node.callee.name)) return;
    const source = literal(node.arguments[0]);
    if (!source) return;
    const other = node.callee.name === "n" ? literal(node.arguments[1]) : null;
    const value = other === null ? source : { one: source, other };
    if (node.callee.name === "n" && (other === null || placeholders(source) !== placeholders(other)))
      errors.push(`${file}:${node.loc.start.line}: plural forms need matching named placeholders`);
    const prior = messages.get(source);
    if (prior && JSON.stringify(prior) !== JSON.stringify(value))
      errors.push(`${file}:${node.loc.start.line}: ${JSON.stringify(source)} mixes singular/plural uses`);
    messages.set(source, value);
  });
}

// Catalog order must not depend on the developer's OS/ICU version.
const english = Object.fromEntries([...messages.entries()].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
function readCatalog(file) {
  const raw = fs.readFileSync(path.join(catalogRoot, file), "utf8");
  const data = JSON.parse(raw);
  if (!data || typeof data !== "object" || Array.isArray(data))
    throw new Error(`${file}: expected an object`);
  walk(parse(`(${raw})`, { ecmaVersion: "latest" }), (node) => {
    if (node.type !== "ObjectExpression") return;
    const keys = node.properties.map((property) => property.key.value);
    if (new Set(keys).size !== keys.length) errors.push(`${file}: duplicate message or plural key`);
  });
  return data;
}
if (process.argv.includes("--extract")) {
  if (!errors.length) {
    fs.mkdirSync(catalogRoot, { recursive: true });
    fs.writeFileSync(path.join(catalogRoot, "en.json"), JSON.stringify(english, null, 2) + "\n");
  }
} else {
  const saved = readCatalog("en.json");
  if (JSON.stringify(saved) !== JSON.stringify(english))
    errors.push("English catalog is stale; run node contrib/i18n.mjs --extract");
}

if (!process.argv.includes("--extract")) {
  const source = fs.readFileSync(path.join(root, "i18n.js"), "utf8");
  const languages = [...source.matchAll(/\{ code: "([^"]+)"/g)].map((match) => match[1]);
  const files = fs.readdirSync(catalogRoot).filter((file) => file.endsWith(".json"));
  for (const code of languages) {
    const file = `${code}.json`;
    if (!files.includes(file)) { errors.push(`Missing catalog: ${file}`); continue; }
    const data = readCatalog(file);
    const categories = new Intl.PluralRules(code).resolvedOptions().pluralCategories;
    for (const [key, reference] of Object.entries(english)) {
      const translated = data[key];
      const plural = typeof reference === "object";
      const valid = plural
        ? translated && typeof translated === "object" && !Array.isArray(translated) &&
          categories.every((category) => typeof translated[category] === "string" && translated[category].trim()) &&
          Object.keys(translated).every((category) => categories.includes(category))
        : typeof translated === "string" && translated.trim();
      if (!valid) { errors.push(`${file}: missing/invalid ${JSON.stringify(key)}`); continue; }
      for (const text of plural ? Object.values(translated) : [translated])
        if (placeholders(text) !== placeholders(key))
          errors.push(`${file}: changed placeholders in ${JSON.stringify(key)}`);
    }
    for (const key of Object.keys(data))
      if (!Object.hasOwn(english, key)) errors.push(`${file}: unused ${JSON.stringify(key)}`);
  }
  for (const file of files)
    if (!languages.includes(file.slice(0, -5))) errors.push(`Unregistered catalog: ${file}`);
}

if (errors.length) {
  process.stderr.write(errors.join("\n") + "\n");
  process.exitCode = 1;
} else {
  process.stdout.write(`${messages.size} source messages ${process.argv.includes("--extract") ? "extracted" : "and catalogs checked"}.\n`);
}
