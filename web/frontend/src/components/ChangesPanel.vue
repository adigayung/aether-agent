<script setup>
// Changes / Diff + Result (#52 rework). Data dari event SSE AETHER (#51).
// TIDAK ada diff engine di frontend: hanya menampilkan perubahan yang
// dilaporkan AETHER. Diff detail ditampilkan bila payload menyediakannya.
import { computed, ref } from "vue";

const props = defineProps({
  changes: { type: Array, default: () => [] },
  validation: { type: Object, default: () => ({}) },
});

const selected = ref(null);
const emit = defineEmits(["open-file"]);

// Label/kelas tag dari kind perubahan (created/modified/deleted/...).
function tagClass(c) {
  const k = (c.kind || "change").toLowerCase();
  if (k.includes("creat") || k.includes("add") || k.includes("new")) return "created";
  if (k.includes("delet") || k.includes("remov")) return "failed";
  if (k.includes("modif") || k.includes("edit") || k.includes("updat")) return "modified";
  return "idle";
}
function tagLabel(c) {
  const k = (c.kind || "change").toLowerCase();
  if (k.includes("creat") || k.includes("add") || k.includes("new")) return "created";
  if (k.includes("delet") || k.includes("remov")) return "deleted";
  if (k.includes("modif") || k.includes("edit") || k.includes("updat")) return "modified";
  return k;
}

const validationLabel = computed(() => {
  const s = props.validation.state;
  if (s === "ok") return "Passed";
  if (s === "err") return "Failed";
  if (s === "running") return "Running";
  return "Pending";
});

const validationClass = computed(() => {
  const s = props.validation.state;
  if (s === "ok") return "ok";
  if (s === "err") return "err";
  if (s === "running") return "running";
  return "";
});

// Diff dari payload event (bila AETHER menyediakannya). Tidak mengarang diff.
const selectedDiff = computed(() => {
  if (!selected.value) return null;
  const c = selected.value;
  return {
    path: c.path || c.detail || "(unknown)",
    additions: c.additions ?? null,
    deletions: c.deletions ?? null,
    diff: c.diff || null,
  };
});
</script>

<template>
  <section class="block">
    <div class="block-head">
      <div class="block-title">Changes</div>
      <span class="chip chip-sm">{{ changes.length }} files</span>
    </div>

    <!-- Body scroll owner (meniru pola .ex-head/.explorer): header tetap,
         daftar perubahan + diff + result mengalir & scroll di area ini. -->
    <div class="changes-body">
      <div v-if="!changes.length" class="ex-empty">No changes yet.</div>

      <template v-else>
        <div
          v-for="(c, i) in changes"
          :key="i"
          class="file-row"
          :class="{ selected: selected === c }"
          @click="selected = c"
        >
          <span class="file-ico">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
          </span>
          <div class="file-body">
            <div class="file-name" :title="c.path || c.detail">{{ c.path || c.detail || "(unknown)" }}</div>
            <div class="file-meta">
              <div class="file-tags">
                <span class="tag" :class="tagClass(c)">{{ tagLabel(c) }}</span>
                <span v-if="c.additions != null || c.deletions != null" class="tag neutral"
                  >+{{ c.additions || 0 }}/-{{ c.deletions || 0 }}</span
                >
              </div>
            </div>
          </div>
          <button class="file-edit" type="button" title="Edit" @click.stop="emit('open-file', c)">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
          </button>
        </div>

        <!-- Diff file terpilih (bila tersedia dari AETHER). -->
        <div v-if="selectedDiff" class="cp-diff">
          <div class="cp-diff-path">{{ selectedDiff.path }}</div>
          <div v-if="selectedDiff.diff" class="cp-diff-body">
            <pre>{{ selectedDiff.diff }}</pre>
          </div>
          <div v-else class="cp-diff-note">Diff detail is not available from AETHER.</div>
        </div>

        <!-- Result ringkas. -->
        <div class="cp-result">
          <span class="cp-result-label">Result</span>
          <span class="cp-result-val">
            {{ changes.length }} file(s) changed
            <span class="cp-result-validation" :class="validationClass">· Validation {{ validationLabel }}</span>
          </span>
        </div>
      </template>
    </div>
  </section>
</template>
