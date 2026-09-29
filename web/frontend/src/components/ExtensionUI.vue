<script setup>
// Extension UI System — generic declarative renderers (Task 05).
//
// Satu runtime generik untuk semua Extension (tanpa hardcode extension_id):
//   - Form (config-driven declarative)
//   - Table (columns/rows/pagination)
//   - Chart (bar/line/area/pie/scatter via HTML/CSS generic)
//   - Modal / Panel / Wizard / Viewer / ResultRenderer / Custom View Bridge
//
// Arsitektur:
//   Extension -> context.ui.register -> UICatalog -> backend /api/extensions/ui
//                                            \-> Vue runtime generik ini
//
// Prinsip:
//   - Tidak ada `if extension_id === ...`
//   - Rendering ditentukan oleh: UI type / renderer id / schema / mime_type
//   - Secret tidak pernah ditampilkan
//   - Validation: frontend awal, backend authoritative
import { computed, reactive, ref, watch } from "vue";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fieldWidgetType(field) {
  const t = String(field.field_type || field.type || "").toLowerCase();
  if (t === "secret") return "secret";
  if (t === "checkbox" || t === "boolean") return "checkbox";
  if (t === "select" || t === "enum") return "select";
  if (t === "number") return "number";
  if (t === "url") return "url";
  if (t === "path") return "path";
  if (t === "json") return "json";
  if (t === "list") return "list";
  return "text";
}

// Map config type -> field validation hint (ditampilkan di UI, backend tetap autoritas)
function validateField(field, value) {
  const type = String(field.type || "").toLowerCase();
  const required = Boolean(field.required);
  if (required && (value === "" || value == null || (Array.isArray(value) && value.length === 0))) {
    return "Required";
  }
  if (value === "" || value == null) return "";
  if (type === "integer") {
    if (!Number.isInteger(Number(value))) return "Must be integer";
  } else if (type === "number") {
    if (Number.isNaN(Number(value))) return "Must be number";
  } else if (type === "boolean") {
    if (typeof value !== "boolean" && value !== "true" && value !== "false") return "Must be boolean";
  } else if (type === "url") {
    try {
      const u = new URL(String(value));
      if (!u.protocol.startsWith("http")) return "Must be valid URL";
    } catch {
      return "Must be valid URL";
    }
  } else if (type === "enum") {
    const choices = field.choices || [];
    if (choices.length && !choices.includes(value)) return `Must be one of ${choices.join(", ")}`;
  } else if (type === "json") {
    try {
      if (typeof value === "string") JSON.parse(value);
    } catch {
      return "Must be valid JSON";
    }
  }
  return "";
}

const props = defineProps({
  // Generic mode: either pass `contribution` (from UICatalog) or `schema` + `typeOverride`
  contribution: { type: Object, default: null },
  schema: { type: Object, default: null },
  // For result rendering: structured result payload
  result: { type: Object, default: null },
  // For artifact: {artifact_id, mime_type, size, name, metadata}
  artifact: { type: Object, default: null },
});

const emit = defineEmits(["submit", "action", "close", "update"]);

// Resolve effective schema/type from contribution if provided
const effectiveSchema = computed(() => {
  if (props.schema) return props.schema;
  if (props.contribution && props.contribution.schema) return props.contribution.schema;
  if (props.contribution && props.contribution.props && props.contribution.props.schema) return props.contribution.props.schema;
  return null;
});

const uiType = computed(() => {
  if (props.contribution && props.contribution.type) return String(props.contribution.type).toLowerCase();
  if (effectiveSchema.value && effectiveSchema.value.type) return String(effectiveSchema.value.type).toLowerCase();
  return "panel";
});

const title = computed(() => {
  if (props.contribution && props.contribution.title) return props.contribution.title;
  if (effectiveSchema.value && effectiveSchema.value.title) return effectiveSchema.value.title;
  return "";
});

const description = computed(() => {
  if (props.contribution && props.contribution.description) return props.contribution.description;
  if (effectiveSchema.value && effectiveSchema.value.description) return effectiveSchema.value.description;
  return "";
});

const fields = computed(() => {
  const s = effectiveSchema.value;
  if (!s) return [];
  if (Array.isArray(s.fields)) return s.fields;
  return [];
});

// Form state (declarative)
const formValues = reactive({});
const formErrors = reactive({});
const formSubmitting = ref(false);

watch(
  fields,
  (list) => {
    // Initialize form values from defaults (but not for secret)
    list.forEach((f) => {
      if (!(f.key in formValues)) {
        if (f.secret) {
          formValues[f.key] = "";
        } else if (f.default !== undefined && f.default !== null) {
          if (f.field_type === "json" && typeof f.default === "object") {
            formValues[f.key] = JSON.stringify(f.default, null, 2);
          } else {
            formValues[f.key] = f.default;
          }
        } else {
          // default per widget
          const w = fieldWidgetType(f);
          if (w === "checkbox") formValues[f.key] = false;
          else if (w === "number") formValues[f.key] = "";
          else if (w === "list") formValues[f.key] = [];
          else formValues[f.key] = "";
        }
      }
    });
  },
  { immediate: true }
);

function onFieldInput(field, value) {
  formValues[field.key] = value;
  const err = validateField(field, value);
  if (err) formErrors[field.key] = err;
  else delete formErrors[field.key];
  emit("update", { key: field.key, value });
}

function submitForm() {
  // validate all
  let hasError = false;
  fields.value.forEach((f) => {
    const err = validateField(f, formValues[f.key]);
    if (err) {
      formErrors[f.key] = err;
      hasError = true;
    }
  });
  if (hasError) return;
  const payload = {};
  fields.value.forEach((f) => {
    let v = formValues[f.key];
    // Coerce number
    if (f.type === "integer") {
      v = v === "" ? null : Number.parseInt(String(v), 10);
    } else if (f.type === "number") {
      v = v === "" ? null : Number(v);
    } else if (f.type === "boolean") {
      v = Boolean(v);
    } else if (f.type === "json" && typeof v === "string" && v.trim()) {
      try {
        v = JSON.parse(v);
      } catch {
        // keep as string; backend will validate
      }
    } else if (f.type === "list" && typeof v === "string") {
      try {
        const parsed = JSON.parse(v);
        if (Array.isArray(parsed)) v = parsed;
        else v = String(v).split(",").map((s) => s.trim()).filter(Boolean);
      } catch {
        v = String(v).split(",").map((s) => s.trim()).filter(Boolean);
      }
    }
    payload[f.key] = v;
  });
  emit("submit", payload);
}

// ---------------------------------------------------------------------------
// Table rendering (generic)
// ---------------------------------------------------------------------------
const tableData = computed(() => {
  if (props.result && props.result.data) return props.result.data;
  const sch = effectiveSchema.value;
  if (sch && sch.type === "table") return sch;
  if (props.contribution && props.contribution.props) return props.contribution.props;
  return null;
});

const tableColumns = computed(() => {
  const d = tableData.value;
  if (!d) return [];
  if (Array.isArray(d.columns)) return d.columns;
  return [];
});

const tableRows = computed(() => {
  const d = tableData.value;
  if (!d) return [];
  if (Array.isArray(d.rows)) return d.rows;
  return [];
});

// ---------------------------------------------------------------------------
// Chart rendering (generic, CSS-based — tanpa library baru)
// ---------------------------------------------------------------------------
const chartData = computed(() => {
  if (props.result && props.result.data) return props.result.data;
  const sch = effectiveSchema.value;
  if (sch && (sch.type === "chart" || sch.datasets)) return sch;
  if (props.contribution && props.contribution.props) return props.contribution.props;
  return null;
});

// ---------------------------------------------------------------------------
// Viewer resolution (generic)
// ---------------------------------------------------------------------------
const viewerType = computed(() => {
  if (props.result && (props.result.type || props.result.renderer)) return String(props.result.type || props.result.renderer);
  const sch = effectiveSchema.value;
  if (sch && sch.viewer_type) return String(sch.viewer_type);
  if (props.contribution && props.contribution.props && props.contribution.props.viewer_type) return String(props.contribution.props.viewer_type);
  return "";
});

const artifactMime = computed(() => {
  if (props.artifact && props.artifact.mime_type) return String(props.artifact.mime_type);
  if (props.result && props.result.artifact && props.result.artifact.mime_type) return String(props.result.artifact.mime_type);
  return "";
});

// Wizard state
const wizardStep = ref(0);
const wizardSteps = computed(() => {
  const s = effectiveSchema.value;
  if (s && Array.isArray(s.steps)) return s.steps;
  if (props.contribution && props.contribution.props && Array.isArray(props.contribution.props.steps)) return props.contribution.props.steps;
  return [];
});

function wizardNext() {
  if (wizardStep.value < wizardSteps.value.length - 1) wizardStep.value += 1;
}
function wizardPrev() {
  if (wizardStep.value > 0) wizardStep.value -= 1;
}

// Modal state
const modalOpen = ref(true);
function closeModal() {
  modalOpen.value = false;
  emit("close");
}

// Panel placement hint
const panelPlacement = computed(() => {
  if (props.contribution && props.contribution.props && props.contribution.props.placement) return String(props.contribution.props.placement);
  if (props.contribution && props.contribution.props && props.contribution.props.slot) return String(props.contribution.props.slot);
  return "";
});
</script>

<template>
  <div class="ext-ui-root">
    <!-- Header (generic, no hardcode) -->
    <div v-if="title || description" class="ext-ui-head">
      <div class="ext-ui-title">{{ title || contribution && contribution.id || "Extension UI" }}</div>
      <div v-if="description" class="ext-ui-desc">{{ description }}</div>
      <div v-if="panelPlacement" class="ext-ui-meta">Placement: {{ panelPlacement }}</div>
    </div>

    <!-- Error banner for form -->
    <slot name="banner"></slot>

    <!-- ===================== Form Renderer ===================== -->
    <div v-if="uiType === 'form' || (fields.length > 0 && (uiType === 'panel' || uiType === 'wizard'))" class="ext-ui-form">
      <div v-for="field in fields" :key="field.key" class="ext-ui-field">
        <label class="ext-ui-label">
          {{ field.title || field.key }}
          <span v-if="field.required" class="ext-ui-req">*</span>
        </label>
        <div v-if="field.description" class="ext-ui-field-desc">{{ field.description }}</div>
        <!-- Secret : always password + configured hint, never value -->
        <template v-if="fieldWidgetType(field) === 'secret'">
          <input
            :value="formValues[field.key]"
            type="password"
            class="ext-ui-input"
            :placeholder="field.configured ? '•••••• (configured)' : 'Not configured'"
            @input="onFieldInput(field, $event.target.value)"
          />
          <div class="ext-ui-hint">{{ field.configured ? "● Configured" : "○ Not configured" }}</div>
        </template>
        <template v-else-if="fieldWidgetType(field) === 'checkbox'">
          <label class="ext-ui-check">
            <input type="checkbox" :checked="Boolean(formValues[field.key])" @change="onFieldInput(field, $event.target.checked)" />
            <span>{{ field.title || field.key }}</span>
          </label>
        </template>
        <template v-else-if="fieldWidgetType(field) === 'select'">
          <select class="ext-ui-input" :value="formValues[field.key]" @change="onFieldInput(field, $event.target.value)">
            <option value="" disabled>Select {{ field.key }}</option>
            <option v-for="c in (field.choices || [])" :key="String(c)" :value="c">{{ c }}</option>
          </select>
        </template>
        <template v-else-if="fieldWidgetType(field) === 'number'">
          <input class="ext-ui-input" type="number" :value="formValues[field.key]" @input="onFieldInput(field, $event.target.value)" />
        </template>
        <template v-else-if="fieldWidgetType(field) === 'url'">
          <input class="ext-ui-input" type="url" :value="formValues[field.key]" placeholder="https://example.com" @input="onFieldInput(field, $event.target.value)" />
        </template>
        <template v-else-if="fieldWidgetType(field) === 'path'">
          <input class="ext-ui-input" type="text" :value="formValues[field.key]" placeholder="/path/to/file" @input="onFieldInput(field, $event.target.value)" />
        </template>
        <template v-else-if="fieldWidgetType(field) === 'json'">
          <textarea
            class="ext-ui-input ext-ui-textarea"
            :value="formValues[field.key]"
            rows="4"
            placeholder='{&quot;key&quot;: &quot;value&quot;}'
            @input="onFieldInput(field, $event.target.value)"
          ></textarea>
        </template>
        <template v-else-if="fieldWidgetType(field) === 'list'">
          <input
            class="ext-ui-input"
            type="text"
            :value="Array.isArray(formValues[field.key]) ? formValues[field.key].join(', ') : formValues[field.key]"
            placeholder="a, b, c  or JSON array"
            @input="onFieldInput(field, $event.target.value)"
          />
        </template>
        <template v-else>
          <input class="ext-ui-input" type="text" :value="formValues[field.key]" @input="onFieldInput(field, $event.target.value)" />
        </template>
        <div v-if="formErrors[field.key]" class="ext-ui-err">{{ formErrors[field.key] }}</div>
      </div>
      <div class="ext-ui-actions">
        <button class="btn-aether btn-primary-a" :disabled="formSubmitting" @click="submitForm">Save</button>
        <button class="btn-aether btn-ghost-a" @click="emit('close')">Cancel</button>
      </div>
    </div>

    <!-- ===================== Table Renderer (generic) ===================== -->
    <div v-else-if="uiType === 'table'" class="ext-ui-table-wrap">
      <div v-if="tableColumns.length || tableRows.length" class="ext-ui-table-scroll">
        <table class="ext-ui-table">
          <thead v-if="tableColumns.length">
            <tr>
              <th v-for="col in tableColumns" :key="col.key || col.field || col.title">
                {{ col.title || col.label || col.key || col.field || "" }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, ri) in tableRows" :key="ri">
              <td v-for="col in (tableColumns.length ? tableColumns : Object.keys(row || {}).map(k => ({key: k})))" :key="col.key || col.field">
                {{ (row && typeof row === 'object' && !Array.isArray(row)) ? (row[col.key || col.field] ?? "") : (Array.isArray(row) ? row[tableColumns.indexOf(col)] ?? "" : String(row ?? "")) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="wb-empty">No data</div>
      <div v-if="tableData && tableData.pagination" class="ext-ui-pagination">
        Page {{ tableData.pagination.page || 1 }} / {{ tableData.pagination.total_pages || 1 }}
      </div>
    </div>

    <!-- ===================== Chart Renderer (generic, no hardcode extension) ===================== -->
    <div v-else-if="uiType === 'chart'" class="ext-ui-chart">
      <div v-if="chartData" class="ext-ui-chart-head">
        <span class="ext-ui-chart-type">{{ chartData.type || "chart" }}</span>
        <span v-if="chartData.title" class="ext-ui-chart-title"> — {{ chartData.title }}</span>
      </div>
      <div v-if="chartData && Array.isArray(chartData.labels) && Array.isArray(chartData.datasets)" class="ext-ui-chart-bars">
        <div v-for="(ds, di) in chartData.datasets" :key="di" class="ext-ui-chart-dataset">
          <div class="ext-ui-chart-label">{{ ds.label || `Dataset ${di+1}` }}</div>
          <div class="ext-ui-chart-row">
            <div
              v-for="(val, vi) in ds.data"
              :key="vi"
              class="ext-ui-chart-bar"
              :style="{ height: String(Math.max(4, Math.min(100, (Number(val) / Math.max(1, Math.max(...ds.data))) * 100)) + '%') }"
              :title="(chartData.labels && chartData.labels[vi] || String(vi)) + ': ' + String(val)"
            >
              <span class="ext-ui-chart-bar-val">{{ val }}</span>
              <span class="ext-ui-chart-bar-lbl">{{ (chartData.labels && chartData.labels[vi]) || "" }}</span>
            </div>
          </div>
        </div>
      </div>
      <div v-else-if="chartData" class="wb-empty">Chart data</div>
    </div>

    <!-- ===================== Viewer (generic, mime-aware) ===================== -->
    <div v-else-if="uiType === 'viewer'" class="ext-ui-viewer">
      <div class="ext-ui-viewer-type">Viewer: {{ viewerType || "viewer" }} <span v-if="artifactMime">({{ artifactMime }})</span></div>
      <div v-if="artifact" class="ext-ui-artifact">
        <div class="ext-ui-artifact-name">{{ artifact.name || artifact.artifact_id || "" }}</div>
        <div class="ext-ui-artifact-meta">
          <span v-if="artifact.mime_type">{{ artifact.mime_type }}</span>
          <span v-if="artifact.size"> — {{ artifact.size }} bytes</span>
        </div>
      </div>
      <div v-if="result && result.data" class="ext-ui-viewer-payload">
        <pre class="ext-ui-pre">{{ typeof result.data === 'string' ? result.data : JSON.stringify(result.data, null, 2) }}</pre>
      </div>
      <div v-else-if="effectiveSchema" class="ext-ui-viewer-payload">
        <pre class="ext-ui-pre">{{ JSON.stringify(effectiveSchema, null, 2) }}</pre>
      </div>
    </div>

    <!-- ===================== Result Renderer (generic) ===================== -->
    <div v-else-if="uiType === 'result_renderer' || uiType === 'result'" class="ext-ui-result">
      <div v-if="result" class="ext-ui-result-body">
        <div class="ext-ui-result-renderer">Renderer: {{ result.renderer || result.type || "result" }}</div>
        <pre v-if="result.data" class="ext-ui-pre">{{ typeof result.data === 'string' ? result.data : JSON.stringify(result.data, null, 2) }}</pre>
        <div v-if="result.artifact" class="ext-ui-artifact">
          <div class="ext-ui-artifact-name">{{ result.artifact.name || result.artifact.artifact_id || "" }}</div>
          <div class="ext-ui-artifact-meta">{{ result.artifact.mime_type || "" }}</div>
        </div>
      </div>
      <div v-else class="wb-empty">No result</div>
    </div>

    <!-- ===================== Modal (generic) ===================== -->
    <div v-else-if="uiType === 'modal'" class="ext-ui-modal">
      <div v-if="modalOpen" class="modal-backdrop" @click.self="closeModal">
        <div class="modal ext-ui-modal-box" role="dialog" aria-modal="true">
          <div class="modal-head">
            <div class="modal-title">{{ title || "Modal" }}</div>
            <button class="close-x" type="button" title="Close" @click="closeModal">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12" /></svg>
            </button>
          </div>
          <div class="modal-body">
            <slot>
              <pre v-if="effectiveSchema" class="ext-ui-pre">{{ JSON.stringify(effectiveSchema, null, 2) }}</pre>
            </slot>
          </div>
          <div class="modal-actions">
            <button class="btn-aether btn-primary-a" @click="emit('action', { id: 'submit' })">Submit</button>
            <button class="btn-aether btn-ghost-a" @click="closeModal">Close</button>
          </div>
        </div>
      </div>
      <div v-else class="wb-empty">Modal closed</div>
    </div>

    <!-- ===================== Wizard (generic multi-step) ===================== -->
    <div v-else-if="uiType === 'wizard'" class="ext-ui-wizard">
      <div class="ext-ui-wizard-head">Step {{ wizardStep + 1 }} / {{ wizardSteps.length || 1 }}</div>
      <div v-if="wizardSteps.length" class="ext-ui-wizard-body">
        <div class="ext-ui-wizard-step-title">{{ wizardSteps[wizardStep] && (wizardSteps[wizardStep].title || wizardSteps[wizardStep].label) || `Step ${wizardStep+1}` }}</div>
        <pre v-if="wizardSteps[wizardStep]" class="ext-ui-pre">{{ JSON.stringify(wizardSteps[wizardStep], null, 2) }}</pre>
      </div>
      <div v-else class="wb-empty">Wizard — no steps defined</div>
      <div class="ext-ui-actions">
        <button class="btn-aether btn-ghost-a" :disabled="wizardStep === 0" @click="wizardPrev">Previous</button>
        <button v-if="wizardStep < wizardSteps.length - 1" class="btn-aether btn-primary-a" @click="wizardNext">Next</button>
        <button v-else class="btn-aether btn-primary-a" @click="emit('submit', { wizard: true, step: wizardStep })">Submit</button>
        <button class="btn-aether btn-ghost-a" @click="emit('close')">Cancel</button>
      </div>
    </div>

    <!-- ===================== Panel (generic) ===================== -->
    <div v-else-if="uiType === 'panel'" class="ext-ui-panel">
      <slot>
        <div v-if="effectiveSchema"><pre class="ext-ui-pre">{{ JSON.stringify(effectiveSchema, null, 2) }}</pre></div>
        <div v-else class="wb-empty">Panel — no content</div>
      </slot>
    </div>

    <!-- ===================== Action (capability reference) ===================== -->
    <div v-else-if="uiType === 'action'" class="ext-ui-action">
      <button
        class="btn-aether btn-primary-a"
        @click="emit('action', { id: (contribution && contribution.id) || (effectiveSchema && effectiveSchema.id) || 'action' })"
      >
        {{ title || "Action" }}
      </button>
      <div class="ext-ui-hint">Action delegates to capability/tool/command via existing execution path</div>
    </div>

    <!-- ===================== Custom View (bridge, no hardcode) ===================== -->
    <div v-else-if="uiType === 'custom_view'" class="ext-ui-custom">
      <div class="ext-ui-custom-head">Custom View: {{ contribution && contribution.id || title || "custom" }}</div>
      <div v-if="contribution && contribution.entry" class="ext-ui-hint">Entry: {{ contribution.entry }}</div>
      <slot name="custom"></slot>
      <div class="wb-empty">Custom view bridge — extension provides HTML/CSS/JS via single runtime contract</div>
    </div>

    <!-- Fallback for other viewer sub-types (image, video, etc.) -->
    <div v-else-if="viewerType || artifactMime || result" class="ext-ui-viewer">
      <div class="ext-ui-hint">Renderer: {{ viewerType || artifactMime || (result && result.renderer) || uiType }}</div>
      <pre v-if="result" class="ext-ui-pre">{{ JSON.stringify(result, null, 2) }}</pre>
    </div>

    <!-- Slot for callers that want to inject raw content -->
    <slot></slot>
  </div>
</template>

<style scoped>
.ext-ui-root { display: grid; gap: 14px; }
.ext-ui-head { display: grid; gap: 6px; }
.ext-ui-title { font-size: 15px; font-weight: 700; }
.ext-ui-desc { color: var(--text-dim); font-size: 13px; }
.ext-ui-meta { color: var(--text-faint); font-size: 11px; font-family: var(--mono); }

.ext-ui-form { display: grid; gap: 12px; }
.ext-ui-field { display: grid; gap: 6px; }
.ext-ui-label { font-size: 12.5px; font-weight: 600; }
.ext-ui-req { color: var(--err); margin-left: 4px; }
.ext-ui-field-desc { color: var(--text-faint); font-size: 11.5px; }
.ext-ui-input { width: 100%; padding: 9px 12px; border-radius: 9px; border: 1px solid var(--border); background: var(--bg-elev); color: var(--text); font-size: 12.5px; font-family: var(--mono); }
.ext-ui-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(139,92,246,0.18); }
.ext-ui-textarea { resize: vertical; }
.ext-ui-check { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.ext-ui-hint { color: var(--text-faint); font-size: 11.5px; }
.ext-ui-err { color: var(--err); font-size: 11.5px; }
.ext-ui-actions { display: flex; gap: 8px; flex-wrap: wrap; }

.ext-ui-table-wrap { display: grid; gap: 8px; }
.ext-ui-table-scroll { overflow: auto; border: 1px solid var(--border); border-radius: 10px; }
.ext-ui-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.ext-ui-table th { text-align: left; padding: 8px 10px; background: rgba(255,255,255,0.04); border-bottom: 1px solid var(--border); font-weight: 600; }
.ext-ui-table td { padding: 7px 10px; border-bottom: 1px solid var(--border-soft); }
.ext-ui-pagination { color: var(--text-faint); font-size: 11.5px; text-align: right; }

.ext-ui-chart { display: grid; gap: 10px; }
.ext-ui-chart-head { font-size: 13px; font-weight: 600; }
.ext-ui-chart-type { text-transform: uppercase; font-family: var(--mono); }
.ext-ui-chart-bars { display: grid; gap: 12px; }
.ext-ui-chart-dataset { display: grid; gap: 6px; }
.ext-ui-chart-label { font-size: 12px; font-weight: 600; }
.ext-ui-chart-row { display: flex; align-items: end; gap: 6px; min-height: 80px; padding: 8px; background: rgba(255,255,255,0.03); border-radius: 10px; }
.ext-ui-chart-bar { flex: 1 1 0; display: grid; align-content: end; justify-items: center; gap: 4px; min-width: 28px; background: rgba(139,92,246,0.22); border-radius: 6px 6px 0 0; padding: 6px 2px; }
.ext-ui-chart-bar-val { font-size: 11px; font-weight: 700; }
.ext-ui-chart-bar-lbl { font-size: 10px; color: var(--text-faint); }

.ext-ui-viewer { display: grid; gap: 10px; }
.ext-ui-viewer-type { font-size: 12.5px; font-weight: 600; font-family: var(--mono); }
.ext-ui-artifact { display: grid; gap: 4px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; }
.ext-ui-artifact-name { font-weight: 600; }
.ext-ui-artifact-meta { color: var(--text-faint); font-size: 11.5px; font-family: var(--mono); }
.ext-ui-pre { white-space: pre-wrap; word-break: break-word; background: rgba(255,255,255,0.04); border: 1px solid var(--border-soft); border-radius: 9px; padding: 10px 12px; font-size: 12px; font-family: var(--mono); }
.ext-ui-result { display: grid; gap: 10px; }
.ext-ui-result-renderer { font-size: 12px; font-family: var(--mono); color: var(--text-faint); }

.ext-ui-modal-box { min-width: 520px; max-width: 720px; }
.ext-ui-wizard { display: grid; gap: 12px; }
.ext-ui-wizard-head { font-size: 12.5px; font-weight: 600; }
.ext-ui-wizard-step-title { font-size: 13px; font-weight: 600; }
</style>
