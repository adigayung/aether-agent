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
import { renderMarkdown } from "../markdown.js";

const props = defineProps({
  taskId: { type: String, default: "" },
  status: { type: String, default: "" },
  report: { type: String, default: null },
});

defineEmits(["close"]);

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
