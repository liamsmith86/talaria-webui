// Streamed Markdown must match a full parse at every step, however the reply is split.
// Usage: node tests/markdown_stream_fuzz.mjs [runs] [seed]. A failure prints its seed.
import assert from "node:assert/strict";
import process from "node:process";
import fc from "fast-check";
import { marked } from "../src/talaria/static/vendor/marked.js";
import { MarkdownStream } from "../src/talaria/static/markdown-stream.js";

// The options markdown.js sets. Its renderers would apply to both sides of the comparison.
marked.use({ gfm: true, breaks: true });

// Fragments that start, end or change a block, since random characters rarely form one.
const FRAGMENTS = [
  "\n", "\n\n", " ", "  ", "    ", "\t", "\r\n", "a", "word", "Hello there.",
  "1. ", "2) ", "10.", "- ", "* ", "+ ", "- [ ] ", "> ", "# ", "## ",
  "```", "```py\n", "~~~", "`", "**", "*", "_", "~~", "---", "===",
  "|", "| a | b |\n", "|---|---|\n", "| 1 | 2 |\n",
  "[x](http://a.b)", "[r]: http://a.b\n", "[r]", "<div>", "</div>", "<!--", "-->",
  "www.", "a.b", "http://a.b", "http", ":", "//", "-", "--", "'", "\"", "(", ")", "—", "’", "&amp;", "&", "\\", "!", "?", ",", ".", ";", "é", "日本", "😀",
];
const reply = fc
  .array(fc.constantFrom(...FRAGMENTS), { minLength: 5, maxLength: 80, size: "max" })
  .map((parts) => parts.join(""));
const chunks = fc.array(fc.integer({ min: 1, max: 12 }), { minLength: 1, maxLength: 20 });

const [numRuns = 5000, seed = 42] = process.argv.slice(2).map(Number);
fc.assert(
  fc.property(reply, chunks, (source, sizes) => {
    const stream = new MarkdownStream();
    for (let end = 0, step = 0; end < source.length; step++) {
      end += sizes[step % sizes.length];
      const text = source.slice(0, end);
      assert.equal(marked.parser(stream.read(text, true)), marked.parse(text), JSON.stringify(text));
    }
  }),
  { numRuns, seed },
);
