// Markdown-lite renderer (self-contained, tanpa dependency baru).
//
// Dipakai bersama oleh ReportViewer (Agent Report) dan ConsultantChat
// (jawaban Consultant) sehingga tidak ada renderer duplikat. Mendukung:
// heading, paragraf, list (bullet/number), code block, blockquote, hr, dan
// inline (`code`, **bold**, *italic*).
//
// Keamanan: konten di-escape lebih dulu, lalu hanya markup terbatas yang
// dihasilkan. TIDAK menerima HTML mentah dari sumber.

export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Inline Markdown: `code`, **bold**, *italic*.
export function inlineMarkdown(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  return out;
}

// Markdown-lite -> HTML (deterministik, tanpa dependency).
export function renderMarkdown(md) {
  if (md == null) return "";
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let inCode = false;
  let codeBuf = [];
  let listType = null;
  let para = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inlineMarkdown(para.join(" "))}</p>`);
      para = [];
    }
  };
  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inCode) {
        out.push(`<pre class="md-code"><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`);
        codeBuf = [];
        inCode = false;
      } else {
        flushPara();
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeBuf.push(line);
      continue;
    }
    if (!line.trim()) {
      flushPara();
      closeList();
      continue;
    }

    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara();
      closeList();
      const level = m[1].length;
      out.push(`<h${level} class="md-h">${inlineMarkdown(m[2])}</h${level}>`);
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      flushPara();
      closeList();
      out.push('<hr class="md-hr" />');
      continue;
    }
    if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      flushPara();
      if (listType !== "ul") {
        closeList();
        out.push('<ul class="md-ul">');
        listType = "ul";
      }
      out.push(`<li>${inlineMarkdown(m[1])}</li>`);
      continue;
    }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (listType !== "ol") {
        closeList();
        out.push('<ol class="md-ol">');
        listType = "ol";
      }
      out.push(`<li>${inlineMarkdown(m[1])}</li>`);
      continue;
    }
    if ((m = line.match(/^\s*>\s?(.*)$/))) {
      flushPara();
      closeList();
      out.push(`<blockquote class="md-quote">${inlineMarkdown(m[1])}</blockquote>`);
      continue;
    }
    para.push(line.trim());
  }

  if (inCode) {
    out.push(`<pre class="md-code"><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`);
  }
  flushPara();
  closeList();
  return out.join("\n");
}
