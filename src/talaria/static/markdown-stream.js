import { marked } from "./vendor/marked.js";

// Only syntax-free prose can extend a text token without consulting Markdown.
// In particular, punctuation that could complete a URL, entity, or delimiter
// must go through the lexer again.
const prose = /^[\p{L}\p{N} ,.!?;]+$/u;
const plain = (text) =>
  prose.test(text) && !/www\.|^ {0,3}\d{1,9}\.(?: |$)/i.test(text);
const flatItem = (item) =>
  !item.task &&
  !item.loose &&
  !item.text.includes("\n") &&
  (item.tokens.length === 0 ||
    (item.tokens.length === 1 && item.tokens[0].type === "text"));
export const flatList = (token) =>
  token.type === "list" && !token.loose && token.items.every(flatItem);

export class MarkdownStream {
  source = "";
  tokens = [];
  offset = 0;
  count = 0;
  references = false;

  read(source, streaming) {
    const append = source.startsWith(this.source);
    const delta = append ? source.slice(this.source.length) : "";
    let tokens;
    // Final output and replacements always use the authoritative full parser.
    // CR normalization changes raw offsets; reference definitions can change
    // inline meaning anywhere in the document. Keep these on the safe path.
    if (!streaming || !append || this.references || source.includes("\r")) {
      tokens = marked.lexer(source);
      this.offset = this.count = 0;
    } else if (delta && this.extend(delta)) {
      tokens = this.tokens;
    } else {
      const tail = marked.lexer(source.slice(this.offset));
      if (Object.keys(tail.links).length) {
        tokens = marked.lexer(source);
        this.offset = this.count = 0;
      } else {
        tokens = this.tokens.slice(0, this.count).concat(tail);
        tokens.links = tail.links;
      }
    }
    this.source = source;
    this.tokens = tokens;
    this.references = Object.keys(tokens.links || {}).length > 0;
    // Retain two open blocks: appends may turn a paragraph into a setext
    // heading, continue a list/quote, or absorb preceding blank lines. Commit
    // only exact raw offsets, never guessed blank-line boundaries.
    let remaining = 2;
    let keep = tokens.length;
    while (keep > this.count && remaining) {
      if (tokens[--keep].type !== "space") remaining--;
    }
    while (this.count < keep) {
      const raw = tokens[this.count].raw;
      if (!source.startsWith(raw, this.offset)) break;
      this.offset += raw.length;
      this.count++;
    }
    for (const token of tokens) {
      if (token.type === "paragraph" && token.streamPlain === undefined)
        token.streamPlain =
          plain(token.raw) &&
          token.text === token.raw &&
          token.tokens.length === 1 &&
          token.tokens[0].type === "text";
    }
    return tokens;
  }

  extend(delta) {
    const last = this.tokens.at(-1);
    if (!last || !this.source.endsWith(last.raw)) return false;
    const raw = last.raw + delta;
    let next;
    if (
      last.streamPlain &&
      prose.test(delta) &&
      !/www\./i.test(last.raw.slice(-4) + delta) &&
      !/^ {0,3}\d{1,9}\.(?: |$)/.test(raw.slice(0, 14))
    ) {
      next = {
        ...last,
        raw,
        text: raw,
        tokens: [{ ...last.tokens[0], raw, text: raw }],
      };
    } else if (last.type === "code" && !last.codeBlockStyle) {
      // Restrict the shortcut to unindented fences. A possible closing marker
      // is deliberately reparsed, including markers split across deltas.
      const header = /^(`{3,}|~{3,})[^\n]*\n/.exec(last.raw);
      if (
        !header ||
        last.raw.slice(header[0].length).includes(header[1][0]) ||
        delta.includes(header[1][0])
      )
        return false;
      next = {
        ...last,
        raw,
        text: raw.slice(header[0].length).replace(/\n$/, ""),
      };
    } else if (last.type === "table" && last.rows.length > 1) {
      const headerEnd = last.raw.indexOf("\n", last.raw.indexOf("\n") + 1) + 1;
      const rowStart = last.raw.lastIndexOf("\n", last.raw.length - 2) + 1;
      const tail = marked.lexer(
        last.raw.slice(0, headerEnd) + last.raw.slice(rowStart) + delta,
      );
      if (
        tail.length !== 1 ||
        tail[0].type !== "table" ||
        Object.keys(tail.links).length
      )
        return false;
      next = {
        ...last,
        raw,
        rows: last.rows.slice(0, -1).concat(tail[0].rows),
      };
    } else if (flatList(last) && last.items.length > 1) {
      const rowStart = last.raw.lastIndexOf("\n", last.raw.length - 2) + 1;
      const tail = marked.lexer(last.raw.slice(rowStart) + delta);
      // Blank lines, nesting, task markers, and delimiter changes can alter
      // preceding items. Let Marked handle the whole open list in those cases.
      const marker = /^\s*(?:\d+([.)])|([-+*]))/.exec(last.raw);
      const nextMarker = /^\s*(?:\d+([.)])|([-+*]))/.exec(
        last.raw.slice(rowStart),
      );
      if (
        !marker ||
        !nextMarker ||
        marker[1] !== nextMarker[1] ||
        marker[2] !== nextMarker[2] ||
        tail.length !== 1 ||
        !flatList(tail[0]) ||
        Object.keys(tail.links).length ||
        /\n[ \t]*\n/.test(last.raw.slice(rowStart) + delta)
      )
        return false;
      next = {
        ...last,
        raw,
        items: last.items.slice(0, -1).concat(tail[0].items),
      };
    } else return false;
    const links = this.tokens.links;
    this.tokens = this.tokens.slice();
    this.tokens.links = links;
    this.tokens[this.tokens.length - 1] = next;
    return true;
  }
}
