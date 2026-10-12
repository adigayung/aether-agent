// Regression test (Node built-in `assert`, no framework) for useStatics.
//
// Memverifikasi perilaku KRITIS TASK 4 yang sulit dilihat di UI:
//   1. Statistik selalu PER-PROJECT: request menyertakan project_id yang benar.
//   2. Respons BASI (project sudah berganti saat request berjalan) DIBUANG,
//      sehingga data project lama tidak pernah muncul kembali.
//   3. Error pada satu project TIDAK menyisakan data project sebelumnya.
//   4. isCurrent=false saat project berganti -> view tidak menampilkan data lama.
//   5. Project kosong -> tidak ada request & data dibersihkan.
//
// Jalankan: node web/frontend/src/useStatics.test.mjs

import assert from "node:assert/strict";
import { ref, nextTick } from "vue";

// --- Mock fetch (dipakai api.js `request`) ---------------------------------
const calls = [];
let responses = []; // daftar respons berurutan (queue resolver)
global.fetch = (url) => {
  calls.push(url);
  const resolver = responses.shift();
  if (!resolver) throw new Error(`Unexpected fetch: ${url}`);
  return resolver(url);
};

function okResponse(payload) {
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
}
function errResponse(status, message) {
  return Promise.resolve({
    ok: false,
    status,
    text: () => Promise.resolve(JSON.stringify({ error: { message } })),
  });
}
// Respons yang dapat diselesaikan manual (untuk simulasi respons terlambat).
function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return {
    promise,
    finish(payload) {
      resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(payload)),
      });
    },
  };
}

import { useStatics } from "./composables/useStatics.js";

let allGood = true;
function check(name, fn) {
  try {
    fn();
    console.log(`PASS: ${name}`);
  } catch (e) {
    console.error(`FAIL: ${name}\n  ${e.message}`);
    allGood = false;
  }
}

// --- Test 1: request menyertakan project_id yang benar ---------------------
{
  const selectedProjectId = ref("proj-A");
  const s = useStatics({ selectedProjectId });
  responses.push(() => okResponse({ project_id: "proj-A", totals: { total_tasks: 3 } }));
  calls.length = 0;
  await s.refresh();
  check("refresh() memanggil /api/statics dengan project_id", () => {
    assert.equal(calls.length, 1);
    assert.ok(calls[0].includes("/statics?project_id=proj-A"), calls[0]);
  });
  check("data tersimpan setelah respons sukses", () => {
    assert.equal(s.data.value.totals.total_tasks, 3);
    assert.equal(s.isCurrent.value, true);
  });
}

// --- Test 2: respons BASI dari project lama DIBUANG ------------------------
{
  const selectedProjectId = ref("proj-A");
  const s = useStatics({ selectedProjectId });
  const dA = deferred(); // respons project A (lambat)
  const dB = deferred(); // respons project B (cepat)
  responses.push(() => dA.promise, () => dB.promise);
  calls.length = 0;

  const pA = s.refresh(); // mulai request untuk A
  selectedProjectId.value = "proj-B"; // user pindah project
  const pB = s.refresh(); // mulai request untuk B

  dB.finish({ project_id: "proj-B", totals: { total_tasks: 7 } });
  await pB;
  // Respons A (basi) baru datang SETELAH B selesai.
  dA.finish({ project_id: "proj-A", totals: { total_tasks: 999 } });
  await pA;

  check("respons basi tidak menimpa data project baru", () => {
    assert.equal(s.data.value.totals.total_tasks, 7, "data harus milik proj-B");
    assert.equal(s.data.value.project_id, "proj-B");
    assert.equal(s.isCurrent.value, true);
  });
  check("request kedua memakai project_id baru", () => {
    assert.ok(calls[1].includes("project_id=proj-B"), calls[1]);
  });
}

// --- Test 3: error tidak menyisakan data project sebelumnya ----------------
{
  const selectedProjectId = ref("proj-A");
  const s = useStatics({ selectedProjectId });
  responses.push(
    () => okResponse({ project_id: "proj-A", totals: { total_tasks: 5 } }),
    () => errResponse(500, "boom")
  );
  await s.refresh();
  assert.equal(s.data.value.totals.total_tasks, 5);
  await s.refresh(); // berpindah memuat, gagal
  check("error membersihkan data lama (tidak menampilkan sisa)", () => {
    assert.equal(s.data.value, null, "data harus null setelah error");
    assert.equal(s.error.value, "boom");
  });
}

// --- Test 4: isCurrent false saat project berganti -------------------------
{
  const selectedProjectId = ref("proj-A");
  const s = useStatics({ selectedProjectId });
  responses.push(() => okResponse({ project_id: "proj-A", totals: { total_tasks: 5 } }));
  await s.refresh();
  assert.equal(s.isCurrent.value, true);
  selectedProjectId.value = "proj-B";
  check("isCurrent=false begitu project berganti (data lama disembunyikan)", () => {
    assert.equal(s.isCurrent.value, false);
  });
}

// --- Test 5: project kosong -> tidak ada request & data dibersihkan --------
{
  const selectedProjectId = ref("");
  const s = useStatics({ selectedProjectId });
  s.data.value = { stale: true }; // seolah sisa data lama
  s.error.value = "old";
  calls.length = 0;
  await s.refresh();
  check("project kosong: tidak ada request & data/error dibersihkan", () => {
    assert.equal(calls.length, 0);
    assert.equal(s.data.value, null);
    assert.equal(s.error.value, "");
    assert.equal(s.isCurrent.value, false);
  });
}

// --- Test 6: filter membangun query string yang benar ----------------------
{
  const selectedProjectId = ref("proj-A");
  const s = useStatics({ selectedProjectId });
  responses.push(() => okResponse({ project_id: "proj-A" }));
  calls.length = 0;
  await s.setActivity("consultant");
  check("setActivity mengirim activity=consultant", () => {
    assert.ok(calls[0].includes("activity=consultant"), calls[0]);
  });
  responses.push(() => okResponse({ project_id: "proj-A" }));
  await s.setGroupBy("provider");
  check("setGroupBy mengirim group_by=provider", () => {
    assert.ok(calls[1].includes("group_by=provider"), calls[1]);
  });
  responses.push(() => okResponse({ project_id: "proj-A" }));
  await s.setRange({ from: "2024-01-01", to: "2024-01-31" });
  check("setRange mengirim from & to", () => {
    assert.ok(calls[2].includes("from=2024-01-01"), calls[2]);
    assert.ok(calls[2].includes("to=2024-01-31"), calls[2]);
  });
}

// --- Test 7: nilai filter tidak valid dikembalikan ke default --------------
{
  const selectedProjectId = ref("proj-A");
  const s = useStatics({ selectedProjectId });
  responses.push(() => okResponse({ project_id: "proj-A" }));
  await s.setActivity("bogus");
  check("activity tidak valid -> 'all'", () => {
    assert.equal(s.activity.value, "all");
    assert.ok(calls[calls.length - 1].includes("activity=all"));
  });
  responses.push(() => okResponse({ project_id: "proj-A" }));
  await s.setGroupBy("nope");
  check("groupBy tidak valid -> 'day'", () => {
    assert.equal(s.groupBy.value, "day");
    assert.ok(calls[calls.length - 1].includes("group_by=day"));
  });
}

await nextTick();

if (!allGood) {
  console.error("\n=== SOME STATICS CHECKS FAILED ===");
  process.exit(1);
} else {
  console.log("\n=== ALL STATICS TESTS PASSED ===");
}
