import { createHash } from "node:crypto";
import { Buffer } from "node:buffer";
import console from "node:console";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, URL } from "node:url";
import { build } from "esbuild";
import { transformAsync } from "@babel/core";
import htm from "babel-plugin-htm";
import { minify } from "terser";

const root = fileURLToPath(new URL("../", import.meta.url));
const source = path.join(root, "src/talaria/static");
const output = path.join(source, "built");
const check = process.argv.includes("--check");
for (const option of process.argv.slice(2)) {
  if (option !== "--check") throw new Error(`Unknown build option: ${option}`);
}
const sha = (data) => createHash("sha256").update(data).digest("hex");
const relative = (file) => path.relative(root, file).split(path.sep).join("/");

async function files(directory) {
  const entries = await fs.readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.filter((entry) => entry.name !== "built")
    .map((entry) => entry.isDirectory() ? files(path.join(directory, entry.name))
      : [path.join(directory, entry.name)]));
  return nested.flat().sort();
}

const inputs = {};
for (const file of await files(source)) inputs[relative(file)] = sha(await fs.readFile(file));

const templates = {
  name: "compile-htm",
  setup(builder) {
    builder.onResolve({ filter: /^talaria:app$/ }, () => ({ path: "app", namespace: "talaria" }));
    builder.onLoad({ filter: /^app$/, namespace: "talaria" }, () => ({
      contents: 'import "./vendor/prism.js"; export * from "./app.js";', resolveDir: source, loader: "js",
    }));
    builder.onLoad({ filter: /\.js$/ }, async ({ path: file }) => {
      if (path.dirname(file) !== source) return;
      const text = await fs.readFile(file, "utf8");
      if (!text.includes("html`")) return;
      const result = await transformAsync(text, {
        filename: file, sourceFileName: path.basename(file), sourceMaps: true,
        babelrc: false, configFile: false,
        plugins: [[htm, {
          pragma: "__talariaCreateElement", useNativeSpread: true,
          import: { module: file === path.join(source, "lib.js") ? "./vendor/preact.js" : "./lib.js", export: "h" },
        }]],
      });
      const map = Buffer.from(JSON.stringify(result.map)).toString("base64");
      return { contents: result.code + "\n//# sourceMappingURL=data:application/json;base64," + map,
        resolveDir: path.dirname(file), loader: "js" };
    });
    builder.onResolve({ filter: /\/vendor\/htm\.js$/ }, ({ path: file, resolveDir }) => ({
      path: path.resolve(resolveDir, file), sideEffects: false,
    }));
    builder.onResolve({ filter: /\.(woff2|png|svg)$/ }, ({ path: file, resolveDir }) => ({
      path: "./" + path.relative(source, path.resolve(resolveDir, file)).split(path.sep).join("/"),
      external: true,
    }));
  },
};

const document = await fs.readFile(path.join(source, "index.html"), "utf8");
const styles = [...document.matchAll(/href="__TALARIA_BASE__static\/(styles\/[^"]+\.css)"/g)]
  .map((match) => match[1]);
const result = await build({
  absWorkingDir: root,
  entryPoints: { app: "talaria:app" },
  outdir: output, entryNames: "[name]",
  bundle: true, format: "esm", platform: "browser", charset: "utf8",
  target: ["es2022"], treeShaking: true, minify: true,
  sourcemap: "external", legalComments: "linked",
  define: { "process.env.NODE_ENV": '"production"', module: "undefined", global: "undefined" },
  plugins: [templates], write: false,
});
const artifacts = new Map(result.outputFiles.map((file) => [path.basename(file.path), file.contents]));

async function optimizeScripts() {
  await Promise.all([...artifacts.keys()].filter((name) => name.endsWith(".js")).map(async (name) => {
    const sourceMap = artifacts.get(name + ".map");
    const optimized = await minify(Buffer.from(artifacts.get(name)).toString(), {
      ecma: 2022, module: true,
      compress: { passes: 3 }, mangle: true,
      format: { comments: /@license|@preserve|^!/ },
      sourceMap: { content: Buffer.from(sourceMap).toString(), filename: name, url: name + ".map" },
    });
    artifacts.set(name, Buffer.from(optimized.code));
    artifacts.set(name + ".map", Buffer.from(optimized.map));
  }));
}

async function standalone(name, entry, format) {
  const built = await build({ absWorkingDir: root, bundle: true, ...entry, outfile: path.join(output, name),
    minify: true, target: "es2022", format, legalComments: "linked", charset: "utf8",
    write: false, plugins: [templates],
    define: { module: "undefined", global: "undefined" } });
  for (const file of built.outputFiles) artifacts.set(path.basename(file.path), file.contents);
}

await optimizeScripts();
await standalone("boot.js", { entryPoints: [path.join(source, "boot.js")], bundle: false }, "esm");
await standalone("appearance.js", { entryPoints: [path.join(source, "appearance.js")] }, "iife");
await standalone("styles.css", { stdin: {
  contents: styles.map((name) => `@import "./${name}";`).join("\n"),
  resolveDir: source, loader: "css",
} });

const html = document.replace(/\s*<link rel="stylesheet"[^>]+>/g, "")
  .replace('<script src="__TALARIA_BASE__static/appearance.js"></script>',
    '<script src="__TALARIA_BASE__static/appearance.js"></script>\n'
    + '<link rel="stylesheet" href="__TALARIA_BASE__static/styles.css" />')
  .replace(/\s*<script defer[^>]+><\/script>/, "")
  .replace('<script type="module"', '<script type="module" data-store-entry="app.js"');
artifacts.set("index.html", Buffer.from(html));

const outputs = Object.fromEntries([...artifacts].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
  .map(([name, data]) => [name, sha(data)]));
artifacts.set("manifest.json", Buffer.from(JSON.stringify({ version: 1, inputs, outputs }, null, 2) + "\n"));
if (check) {
  const existing = await fs.readdir(output).catch(() => []);
  const stale = existing.filter((name) => !artifacts.has(name));
  for (const [name, data] of artifacts) {
    const saved = await fs.readFile(path.join(output, name)).catch(() => null);
    if (!saved || !Buffer.from(data).equals(saved)) stale.push(name);
  }
  if (stale.length) throw new Error(`Production assets are stale: ${stale.join(", ")}. Run npm run build.`);
} else {
  await fs.rm(output, { recursive: true, force: true });
  await fs.mkdir(output, { recursive: true });
  for (const [name, data] of artifacts) await fs.writeFile(path.join(output, name), data);
}
console.log(`${check ? "Verified" : "Built"} ${artifacts.size} production assets.`);
