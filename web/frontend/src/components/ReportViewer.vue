<script setup>
// Agent Report viewer (#report).
//
// Menampilkan final Agent Report yang berasal dari persistent log
// (`.aether/log/` -> Report API: task_completed.data.result, fallback
// task_finished.data.result). Read-only: TIDAK membuat ReportStore baru dan
// TIDAK memanggil LLM.
//
// Report dirender sebagai Markdown ringan (heading, list, code block, quote,
// inline). Renderer kecil ini self-contained (tanpa dependency baru) karena
// project belum memiliki Markdown renderer.
import { computed } from "vue";

const props = defineProps({
  taskId: { type: String, default: "" },
  status: { type: String, default: "" },
  report: { type: String, default: null },
});

defineEmits(["close"]);

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Inline Markdown: `code`, **bold**, *italic*.
function inline(text) {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  return out;
}

// Markdown-lite -> HTML (deterministik, tanpa dependency).
function renderMarkdown(md) {
  if (md == null) return "";
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let inCode = false;
  let codeBuf = [];
  let listType = null;
  let para = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join(" "))}</p>`);
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
      out.push(`<h${level} class="md-h">${inline(m[2])}</h${level}>`);
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
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (listType !== "ol") {
        closeList();
        out.push('<ol class="md-ol">');
        listType = "ol";
      }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    if ((m = line.match(/^\s*>\s?(.*)$/))) {
      flushPara();
      closeList();
      out.push(`<blockquote class="md-quote">${inline(m[1])}</blockquote>`);
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

const html = computed(() => renderMarkdown(props.report));
const hasReport = computed(() => typeof props.report === "string" && props.report.length > 0);
</script>

<template>
  <div class="modal-backdrop" @click.self="$emit('close')">
    <div class="modal report-m" role="dialog" aria-modal="true">
      <div class="report-head">
        <div class="report-title">Agent Report</div>
        <span v-if="status" class="report-status" :class="status">{{ status }}</span>
        <span v-if="taskId" class="report-id mono" :title="taskId">{{ taskId }}</span>
        <button class="close-x" type="button" title="Close" @click="$emit('close')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>
      <div class="report-body">
        <div v-if="!hasReport" class="wb-empty">No report available for this task.</div>
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div v-else class="md" v-html="html"></div>
      </div>
    </div>
  </div>
</template>
