// Tiny, dependency-free Markdown -> HTML renderer.
//
// Security model: every piece of source text is HTML-escaped *before* any
// markup is introduced, and the only tags this module emits are a fixed,
// known-safe set (p, br, strong, em, del, code, pre, a, ul/ol/li, blockquote,
// h1-h6, hr). Link hrefs are restricted to http/https/mailto. That makes the
// output safe to drop into innerHTML even though answers come from an LLM.
//
// Scope: the common subset that chat answers actually use - headings, bold,
// italic, strikethrough, inline + fenced code, links, ordered/unordered lists,
// blockquotes, horizontal rules, and paragraphs. It is intentionally not a
// full CommonMark implementation (e.g. no nested lists or reference links).

// Placeholder token used to stash inline-code spans while the surrounding text
// is formatted, then restore them. It contains no Markdown-significant
// characters, so the formatting passes leave it untouched.
const codeToken = i => `@@CODE${i}@@`;
const CODE_TOKEN_RE = /@@CODE(\d+)@@/g;

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Inline formatting. Input is already HTML-escaped text for a single block.
function renderInline(escaped) {
  // Pull inline code spans out first so their contents aren't treated as
  // markup, then splice them back in at the end.
  const codes = [];
  let s = escaped.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return codeToken(codes.length - 1);
  });

  // Links: [text](url) - only allow safe schemes, otherwise neutralize.
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, text, url) => {
    const href = /^(https?:|mailto:)/i.test(url) ? url : "#";
    return `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  });

  s = s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\s][^_]*?)_/g, "$1<em>$2</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>");

  return s.replace(CODE_TOKEN_RE, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

export function render(md) {
  const lines = String(md == null ? "" : md).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let paragraph = [];
  let i = 0;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const html = renderInline(escapeHtml(paragraph.join("\n"))).replace(/\n/g, "<br>");
    out.push(`<p>${html}</p>`);
    paragraph = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block: ``` ... ```
    if (/^```/.test(line)) {
      flushParagraph();
      const code = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) code.push(lines[i++]);
      i++; // consume closing fence (if present)
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    // Blank line ends a paragraph.
    if (/^\s*$/.test(line)) {
      flushParagraph();
      i++;
      continue;
    }

    // Heading: # .. ######
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(escapeHtml(heading[2].trim()))}</h${level}>`);
      i++;
      continue;
    }

    // Horizontal rule: ---, ***, ___
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushParagraph();
      out.push("<hr>");
      i++;
      continue;
    }

    // Blockquote: > ... (collected, then rendered recursively)
    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${render(quote.join("\n"))}</blockquote>`);
      continue;
    }

    // Unordered list: -, *, +
    if (/^\s*[-*+]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
        i++;
      }
      out.push(`<ul>${items.map(it => `<li>${renderInline(escapeHtml(it))}</li>`).join("")}</ul>`);
      continue;
    }

    // Ordered list: 1. or 1)
    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
        i++;
      }
      out.push(`<ol>${items.map(it => `<li>${renderInline(escapeHtml(it))}</li>`).join("")}</ol>`);
      continue;
    }

    paragraph.push(line);
    i++;
  }

  flushParagraph();
  return out.join("\n");
}

// Strip Markdown syntax down to plain prose - used for text handed to TTS so it
// doesn't read "asterisk asterisk" etc. aloud.
export function toPlainText(md) {
  return String(md == null ? "" : md)
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^\s*#{1,6}\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+[.)]\s+/gm, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{2,}/g, "\n")
    .trim();
}
