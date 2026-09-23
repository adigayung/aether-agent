# RIG_BLUEPRINT.md

## Final Reconciliation — MAP_CODE_RIG

**Status:** FINAL ARCHITECTURE BLUEPRINT  
**Purpose:** spesifikasi arsitektur implementable untuk standalone Python tool:

```text
python create_rig_json.py ./my_project ./map_my_project.json
```

**Reconciliation rule:** keputusan akhir dibuat dari evidence study → analisis arsitektur → keputusan desain. DeepSeek, Gemini, dan ChatGPT diperlakukan sebagai proposal, bukan sumber kebenaran.

**Normative language**

- **MUST** — wajib untuk implementasi yang compliant.
- **SHOULD** — default yang direkomendasikan; penyimpangan harus eksplisit.
- **MAY** — opsional.
- **MUST NOT** — dilarang.

---

# 1. Purpose

MAP_CODE_RIG adalah tool standalone yang menghasilkan satu artefak kanonik berupa **Repository Intelligence Graph (RIG)** dalam JSON.

RIG memetakan struktur repository pada level **build/test architecture**, bukan pada level semantic code graph. Representasi harus:

- deterministic;
- reproducible;
- evidence-backed;
- repository-aware;
- machine-readable;
- usable sebagai konteks untuk LLM;
- provider/model agnostic;
- traceable kembali ke source/build/test evidence;
- eksplisit terhadap unknown dan unresolved information.

RIG bukan autonomous coding agent dan bukan code-localization engine. RIG membangun fakta arsitektur dari repository dan artefak build/test; konsumen downstream dapat menggunakan fakta tersebut untuk retrieval dan reasoning.

**Evidence:** RIG paper mendefinisikan RIG sebagai representasi arsitektural deterministik berbasis artefak build/test; schema/core study menempatkan `RIG` sebagai canonical data container; LocAgent study menempatkan code localization pada layer lain.

**Status:** CONFIRMED.

---

# 2. Scope

## 2.1 Core scope

Core MAP_CODE_RIG MUST menghasilkan:

1. repository metadata;
2. build profiles / build-system metadata;
3. `Component`;
4. `Aggregator`;
5. `Runner`;
6. `TestDefinition`;
7. `ExternalPackage`;
8. `PackageManager`;
9. first-class directed `Edge`;
10. `Evidence`;
11. validation diagnostics;
12. unresolved references;
13. canonical deterministic JSON.

## 2.2 Production extractor scope

Deterministic track pada study mendokumentasikan enam entrypoint:

- CMake;
- Maven;
- npm;
- Cargo;
- Go;
- Meson.

CMake adalah reference implementation terkuat karena study mendokumentasikan penggunaan CMake File API, CTest JSON, dan evidence backtrace secara rinci.

Python/pyproject, Gradle, Bazel, SCons, dan build systems lain berada di luar core extractor set awal.

**Status:** CONFIRMED untuk deterministic track yang didokumentasikan study; cakupan “MVP production support” adalah DESIGN DECISION.

**Evidence:** `knowledgebase_study.txt`, bagian deterministic entrypoints; RIG paper §II-D.

## 2.3 Repository discovery

Tool MUST menemukan hanya artefak yang diperlukan untuk membangun RIG:

- build-system markers;
- manifest/lock files;
- build metadata;
- test metadata;
- configuration files yang relevan;
- source files yang secara eksplisit dipetakan oleh build/test evidence.

Tool MUST NOT menganggap seluruh tree source sebagai input semantic graph.

Full repository enumeration bukan requirement core.

**Status:** DESIGN DECISION.

**Reason:** knowledgebase dan npm studies menunjukkan bahwa traversal luas dapat memasukkan dependency/vendor/generated tree secara keliru. RIG paper juga menempatkan build artifacts sebagai sumber fakta utama.

---

# 3. Non-Goals

MAP_CODE_RIG MUST NOT menjadi:

1. AST/symbol graph sebagai authoritative RIG graph;
2. control-flow graph;
3. data-flow graph;
4. taint graph;
5. runtime dependency graph;
6. natural-language knowledge graph;
7. embedding/vector database;
8. code-localization ranking engine;
9. agent reasoning loop;
10. automatic code modification tool;
11. dependency installer;
12. default build/test executor;
13. CI/test-result history system;
14. coverage analyzer;
15. universal language intelligence platform.

AST-level entities such as directory/file/class/function, serta relations `contain`, `import`, `invoke`, `inherit`, adalah domain LocAgent-style downstream, bukan node/edge core RIG.

**Status:** CONFIRMED.

**Evidence:** RIG paper abstraction boundary; LocAgent study §3.1/§A.4.

---

# 4. Architectural Principles

## 4.1 Evidence before interpretation

Build/test machine-readable metadata menjadi sumber fakta utama. Source build files menjadi evidence/fallback untuk fakta yang tidak diekspos metadata, bukan alasan untuk menebak semantik.

## 4.2 No silent repair

Parser failure, malformed configuration, missing output, unresolved dependency, dan incomplete metadata tidak boleh diam-diam diubah menjadi fakta yang tampak valid.

## 4.3 UNKNOWN is valid

`unknown`, missing optional value, atau unresolved reference adalah state yang sah bila evidence tidak mencukupi.

## 4.4 Stable identity

ID tidak boleh berasal dari urutan parsing, counter global, dictionary order, timestamp, UUID, process ID, atau traversal order.

## 4.5 First-class relationships

Hubungan graph adalah object yang memiliki type, endpoint, identity, dan evidence sendiri.

## 4.6 JSON is an interchange view

Canonical JSON adalah representasi machine-readable dari RIG, bukan tempat untuk menyembunyikan struktur graph di dalam string atau alias agresif.

## 4.7 Retrieval is downstream

Retrieval/LLM tidak boleh mengubah authoritative RIG facts.

## 4.8 Provider/model agnostic

Tidak ada ketergantungan pada provider LLM tertentu, model tertentu, temperatur tertentu, atau API inference tertentu dalam graph construction.

---

# 5. System Boundaries

## 5.1 MAP_CODE_RIG

MAP_CODE_RIG bertanggung jawab atas:

- repository discovery;
- build/test artifact acquisition di bawah policy eksplisit;
- deterministic extraction;
- evidence collection;
- canonical normalization;
- identity materialization;
- graph assembly;
- validation;
- canonical JSON serialization;
- optional snapshot persistence/cache.

## 5.2 SPADE

SPADE dalam study berfungsi sebagai deterministic extraction architecture/engine, terutama terlihat pada track CMake dan entrypoints build-system lain.

Dalam MAP_CODE_RIG:

- SPADE-style extraction principles boleh menjadi referensi arsitektur;
- extractor dapat mengadopsi pola `entrypoint -> RIG`;
- SPADE tidak menjadi subsystem kedua yang memiliki schema graph berbeda;
- tidak boleh ada dua authoritative graph builders.

Dengan kata lain, SPADE adalah **reference/extraction boundary**, sedangkan MAP_CODE_RIG adalah produk CLI yang menghasilkan canonical RIG.

**Status:** DESIGN DECISION, konsisten dengan pemisahan schema ↔ extractor pada RIG paper.

## 5.3 LocAgent

LocAgent bertanggung jawab atas:

- code-level graph;
- file/class/function structure;
- AST-derived code relationships;
- graph traversal untuk code localization;
- retrieval tool layer.

LocAgent MAY consume:

- RIG component IDs;
- component source-file lists;
- evidence file/line ranges;
- build/test relationships.

LocAgent MUST NOT rewrite RIG facts.

**Status:** CONFIRMED.

**Evidence:** LocAgent study §3.1, §3.2, §A.4.

## 5.4 LLM / Consumer

LLM adalah consumer atau reasoning layer.

LLM MAY:

- memilih node untuk retrieval;
- meminta traversal downstream;
- menginvestigasi unresolved references;
- menginterpretasikan architecture.

LLM MUST NOT menjadi source of truth untuk:

- node existence;
- dependency existence;
- build-system identity;
- source-file membership;
- test mapping;
- external dependency resolution;
- stable graph IDs.

LLM-generated hypotheses hanya boleh menjadi authoritative setelah ada deterministic corroboration dari repository/build/test evidence.

**Status:** DESIGN DECISION dengan dukungan kuat dari shift deterministic pada knowledgebase/LLM0 studies dan prinsip evidence-backed RIG.

---

# 6. Core Concepts

RIG terdiri dari:

```text
Repository
  └── Build Profile(s)
        ├── Component
        ├── Aggregator
        ├── Runner
        └── TestDefinition

Component ──external──> ExternalPackage ──managed_by──> PackageManager

Any RIG entity ──Evidence linkage──> Evidence

All graph relationships ──> Edge[]
```

Node universe dibedakan antara:

### Core build/test nodes

- `component`
- `aggregator`
- `runner`
- `test`

### Satellite semantic entities

- `external_package`
- `package_manager`
- `evidence`

`utility` dipertahankan hanya sebagai historical/reserved vocabulary dan tidak menjadi active MVP node type.

**Status:** DESIGN DECISION karena formal definition dan implementation schema tidak sepenuhnya konsisten tentang status ExternalPackage/PackageManager dan Utility.

---

# 7. Node Model

## 7.1 Common fields

Setiap core node MUST memiliki:

```text
id
kind
name
identity_key
evidence_ids
```

Optional:

```text
labels
attributes
build_profile_ids
```

### `name`

Human/LLM-readable label. Tidak menjamin uniqueness.

### `identity_key`

Canonical semantic identity yang digunakan untuk menghasilkan `id`.

Identity key MUST NOT bergantung pada display-only name jika name dapat berubah tanpa mengubah logical entity.

**Status:** DESIGN DECISION.

---

# 8. Component Model

`Component` merepresentasikan buildable artifact atau package-level artifact.

Minimum:

```text
id
kind = "component"
name
type
programming_language
source_files
evidence_ids
identity_key
```

`type` mempertahankan vocabulary schema:

- `executable`
- `shared_library`
- `static_library`
- `package_library`
- `vm`
- `interpreted`
- `unknown`

`unknown` MUST tetap tersedia.

`runtime` SHOULD menjadi field terpisah dari language.

Runtime tidak boleh diturunkan hanya karena tebakan language.

Study knowledgebase secara khusus mendokumentasikan:

> UNKNOWN language => UNKNOWN runtime

aturan ini dipertahankan.

### Source files

Semua source paths:

- repository-relative;
- POSIX `/`;
- tanpa `..`;
- tidak boleh absolute host path;
- deterministically sorted;
- duplicate-free.

Generated files MAY dicantumkan bila generated status diketahui.

Placeholder seperti `src` sebagai “source file” dilarang.

---

# 9. Aggregator Model

Aggregator merepresentasikan orchestration target yang mengatur target lain dan bukan artifact utama yang dimodelkan sebagai Component.

Contoh:

- CMake meta target;
- Maven reactor/aggregation;
- npm workspace/top-level orchestration script;
- Meson orchestration target.

Aggregator MUST mempunyai explicit build evidence.

Name yang tampak “group-like” saja tidak cukup.

---

# 10. Runner Model

Runner merepresentasikan command invocation yang tidak perlu dianggap sebagai primary artifact.

Minimum:

```text
id
kind = "runner"
name
evidence_ids
```

Recommended:

```text
command
arguments
working_directory
environment_keys
owner_node_id
purpose
identity_key
```

Sensitive values MUST NOT dipublikasikan ke canonical JSON.

Runner discovery tidak berarti command akan dieksekusi.

---

# 11. TestDefinition Model

TestDefinition merepresentasikan **test definition/invocation**, bukan runtime result.

Minimum:

```text
id
kind = "test"
name
evidence_ids
identity_key
```

Recommended:

```text
framework
test_kind
source_files
runner_node_id
test_executable_node_id
components_being_tested_ids
build_profile_ids
command
arguments
```

Normalized `test_kind` MAY menggunakan:

- `unit`
- `integration`
- `system`
- `e2e`
- `performance`
- `smoke`
- `compiler`
- `unknown`

Original test-kind label SHOULD disimpan bila normalisasi dilakukan.

Relasi `tests` berarti:

> test definition tersebut terdokumentasi/evidenced sebagai penguji target.

Relasi ini MUST NOT dipahami sebagai runtime line/branch coverage.

**Status:** CONFIRMED untuk semantic distinction; normalized enum adalah DESIGN DECISION.

---

# 12. External Package Model

ExternalPackage adalah dependency entity yang berasal dari ecosystem/package manager eksternal.

Minimum:

```text
id
kind = "external_package"
name
package_manager_id
evidence_ids
identity_key
```

Recommended:

```text
ecosystem
coordinate
declared_version
resolved_version
dependency_scope
optional
peer
source
lockfile_path
```

`PackageManager` adalah entity terpisah sehingga:

- satu manager dapat dipakai banyak package;
- ID namespace tidak bertabrakan;
- package identity tidak bergantung pada object nesting.

**Status:** DESIGN DECISION.

**Reason:** schema study menanamkan PackageManager di ExternalPackage dan menggunakan namespace/counter yang tumpang tindih; separation adalah perbaikan struktural untuk canonical graph.

---

# 13. Repository / File / Symbol Model

## 13.1 Repository

Top-level `repo` SHOULD memuat:

```text
name
primary_language
languages
build_profile_ids
snapshot_fingerprint
revision (optional)
vcs (optional)
analysis_status
evidence_ids
```

Absolute host path MUST NOT menjadi bagian canonical JSON.

## 13.2 File

File bukan core graph node.

File direpresentasikan sebagai:

- `source_files` pada Component/TestDefinition;
- evidence locator;
- optional source-path metadata.

## 13.3 Symbol

Class/function/method/module bukan core RIG node.

Symbol graph adalah layer terpisah untuk LocAgent-style retrieval.

**Status:** CONFIRMED.

**Evidence:** RIG paper abstraction boundary; LocAgent node model.

---

# 14. Edge Model

## 14.1 Canonical edge

Setiap edge MUST berupa object:

```json
{
  "id": "edge:...",
  "type": "depends_on",
  "source": "component:...",
  "target": "component:...",
  "role": "build",
  "qualifier": null,
  "evidence_ids": ["evidence:..."],
  "origin_plugin": "cmake"
}
```

Minimum fields:

```text
id
type
source
target
evidence_ids
```

## 14.2 Edge types

Canonical vocabulary:

1. `depends_on`
2. `tests`
3. `includes`
4. `links`
5. `external`

Formal definition mendukung kelima relationship families tersebut, tetapi study tidak memberi semantics yang cukup rinci untuk `includes`, `links`, dan sebagian `external`.

Karena itu:

- `depends_on`, `tests`, `external` adalah MVP active relations;
- `includes` dan `links` adalah supported/reserved relations;
- plugin MUST NOT emit them tanpa explicit deterministic evidence.

**Status:** CONFIRMED untuk vocabulary formal; MVP activation adalah DESIGN DECISION.

## 14.3 `depends_on` roles

Untuk menjaga semantics:

- `build` — artifact/component build dependency;
- `orchestration` — aggregator relationship;
- `runner_input` — explicit runner argument relationship;
- `test_support` — explicit test harness relationship.

Hanya `depends_on` dengan `role = "build"` pada Component-to-Component yang masuk ke formal dependency DAG validation.

**Status:** DESIGN DECISION.

## 14.4 `tests` roles

A `tests` edge MAY carry:

- `subject`
- `harness`
- `executable`

Ini menggantikan fragmented relation fields seperti `test_components`, `components_being_tested`, dan `test_executable` pada canonical edge layer.

Compatibility projection MAY still expose those fields.

## 14.5 `external`

`external` SHOULD be:

```text
Component -> ExternalPackage
```

dan Evidence MUST menunjuk pada manifest/lock/build evidence yang menyebabkan dependency tersebut diketahui.

## 14.6 Edge identity

Semantic edge key:

```text
(
  edge_type,
  source_id,
  target_id,
  role,
  qualifier
)
```

`edge.id = SHA-256(canonical_edge_key)` dengan namespaced prefix.

Tidak ada edge numbering berdasarkan insertion order.

---

# 15. Evidence Model

Setiap node dan edge yang masuk canonical graph MUST memiliki ≥1 evidence reference.

Evidence MAY berasal dari:

- file + line range;
- build-system call stack;
- machine-readable build artifact;
- machine-readable test artifact;
- manifest/lockfile;
- generated metadata yang menjadi input extraction.

Minimum conceptual fields:

```text
id
locator_kind
file_path
start_line
end_line
call_stack
source_ref
text_snippet (optional)
```

Tidak semua field wajib terisi sekaligus.

### Evidence rules

1. No fabricated line number.
2. No fake `file:1` placeholder.
3. Evidence path MUST be repository-relative ketika berasal dari repository.
4. External toolchain evidence tidak boleh diam-diam dipresentasikan sebagai repository source evidence.
5. Secret-bearing snippets SHOULD be omitted or redacted.
6. Evidence identity MUST be deterministic.

`Evidence` study memakai `line`/`call_stack` dan validator memastikan minimal salah satunya tersedia.

**Status:** CONFIRMED.

**Evidence:** formal definition evidence completeness; schemas study validator; knowledgebase evidence policy.

---

# 16. Deterministic Identity

## 16.1 Problem in existing schema

Study schema menunjukkan global `count(1)` counters untuk component, aggregator, runner, test, package, evidence. ID seperti `comp-1` bergantung pada creation order.

Ini bukan deterministic semantic identity.

**Status:** CONFIRMED sebagai problem pada legacy schema.

## 16.2 Final ID policy

Canonical IDs MUST be derived dari semantic identity key + namespace.

Pattern:

```text
<namespace>:<sha256(canonical_identity_key)>
```

Recommended namespaces:

```text
repo:
build:
component:
aggregator:
runner:
test:
external_package:
package_manager:
edge:
evidence:
```

## 16.3 Canonical identity key

Identity key MUST contain only fields that define the logical entity.

Examples:

```text
component|cmake|profile=<profile-key>|target=<target-name>|scope=<repo-relative-scope>
aggregator|npm|workspace=<workspace>|name=<script-or-target>
runner|cmake|profile=<profile-key>|command=<canonical-command>|owner=<owner-id>
test|cmake|profile=<profile-key>|name=<test-name>|command=<canonical-command>|scope=<scope>
external_package|npm|ecosystem=npm|coordinate=axios|scope=runtime
package_manager|ecosystem=npm|name=npm
```

Exact per-plugin identity keys MUST be documented by each extractor.

## 16.4 Identity stability

A component ID SHOULD remain unchanged when:

- unrelated files are added;
- unrelated nodes are added;
- parsing order changes;
- machine operating system changes;
- repository absolute filesystem path changes.

An entity ID MAY change when the logical identity itself changes.

Do NOT include full repository content hash in node ID.

## 16.5 Evidence and edge IDs

Evidence and edge IDs are deterministic too.

**Status:** DESIGN DECISION.

**Reason:** study proves deterministic extraction and exposes counter-based IDs, but does not provide a complete formal stable-ID standard. Therefore deterministic semantic IDs are a required architectural choice, not a claimed fact from study.

---

# 17. Parsing and AST Strategy

## 17.1 Source priority

Final extraction ladder:

```text
machine-readable build/test metadata
        ↓
structured manifest/lock metadata
        ↓
source build-file evidence/fallback parsing
        ↓
UNKNOWN / unresolved
```

Source text must not automatically outrank generated machine-readable build metadata.

## 17.2 CMake

Reference path:

- CMake File API for target/artifact/build graph;
- CTest JSON for tests;
- CMakeLists.txt inspection for properties not represented in File API and for evidence/backtrace context;
- deterministic fallback JSON parsing for custom command/target cases where documented.

Do not recreate a full CMake semantic compiler unless evidence requires it.

## 17.3 Maven/npm/Cargo/Go/Meson

Each plugin MUST prefer the ecosystem's declarative or machine-readable metadata.

The plugin MUST document:

- source priority;
- fallback order;
- unsupported semantics;
- external command use;
- deterministic inputs.

## 17.4 npm-specific correction

The study shows that the legacy npm entrypoint invokes `npm list --json --depth=0` and depends on installed machine state; it also recursively finds `package.json` without adequate contamination boundaries.

Final architecture MUST NOT make installed `node_modules` state the authoritative dependency source.

Preferred sources:

1. `package.json`;
2. lockfile;
3. workspace metadata;
4. explicit deterministic generated metadata, if supplied;
5. `npm` command output only when explicitly enabled and treated as an input artifact.

`node_modules/**/package.json` MUST NOT become repository Components by default.

**Status:** DESIGN DECISION, directly motivated by npm study failure modes.

---

# 18. AST / Symbol Roadmap

No AST/symbol graph is part of core RIG V1.

If introduced later:

```text
RIG
  └── stable bridge to source files
          ↓
      Code Graph
        ├── directory
        ├── file
        ├── class
        ├── function
        └── symbol
```

LocAgent's model provides a reference:

```text
contain
import
invoke
inherit
```

and uses tree-sitter in its indexing implementation.

The future code graph MUST have separate:

- node namespace;
- edge namespace;
- schema/version;
- lifecycle;
- retrieval/index responsibility.

It MUST NOT redefine RIG Component identity.

**Status:** CONFIRMED boundary; future integration is ROADMAP.

---

# 19. Relationship Extraction

Extraction MUST follow “evidence first”.

For every candidate relationship:

1. identify source artifact;
2. identify target artifact;
3. identify semantic relation type;
4. construct deterministic identity key;
5. attach evidence;
6. normalize endpoints;
7. deduplicate;
8. validate endpoint type;
9. emit only when semantics are sufficiently supported.

No relationship is created merely because two entities “look related”.

Examples:

- A compiler flag referring to a target is not automatically a `depends_on`.
- A string containing a package name is not automatically an `external` edge.
- A file import is not a RIG `includes` edge unless the active plugin has explicit semantics for it.
- Two targets produced by the same build step are not automatically dependencies.

---

# 20. Unresolved References

Unresolved references are first-class **diagnostic facts**, not fake graph edges.

Canonical structure:

```json
{
  "id": "unresolved:...",
  "kind": "reference",
  "source_entity_id": "runner:...",
  "reference_text": "some-target",
  "reference_kind": "target|package|test|file|other",
  "candidate_ids": [],
  "reason": "target not present in authoritative build metadata",
  "evidence_ids": ["evidence:..."],
  "status": "unresolved"
}
```

Rules:

- MUST be deterministic;
- MUST include evidence when the unresolved reference came from observed input;
- MUST NOT be converted into a guessed edge;
- candidate list MAY be empty;
- candidate matching MAY be provided by downstream tooling, but authoritative edge creation requires deterministic evidence.

**Status:** DESIGN DECISION.

---

# 21. Malformed / Incomplete Code

Core behavior:

### Fatal

The run cannot safely produce any valid artifact because of:

- invalid project root;
- impossible output destination;
- canonical serializer failure;
- graph invariants that make the output structurally unsafe.

### Recoverable

A plugin may fail partially because:

- malformed build file;
- missing optional metadata;
- missing generated build artifact;
- unavailable optional external command;
- unsupported build construct;
- incomplete test mapping.

The tool SHOULD continue extracting other evidence and write a diagnostic.

### Evidence rule

When a field cannot be determined:

```text
UNKNOWN
```

or omit optional field.

Do not invent a value.

### Node rule

If the existence of a node itself cannot be evidenced, do not emit the node merely to “keep the graph complete”.

### Commit rule

Canonical JSON MUST NOT be committed when required global invariants fail.

Warnings and unresolved information MAY coexist with a valid canonical JSON.

**Status:** DESIGN DECISION.

---

# 22. External Dependencies

External dependency extraction MUST distinguish:

- declared dependency;
- resolved dependency;
- package manager;
- scope;
- optional/peer semantics when explicitly available.

The extractor MUST NOT silently equate:

```text
language == package manager
```

or:

```text
installed package == declared dependency
```

When package manager output is used, its relevant version/configuration becomes part of the extraction input for determinism.

When only declaration is available, `resolved_version` remains unknown rather than fabricated.

---

# 23. Multi-Build-System Repository Model

A repository MAY contain multiple independent build systems.

Final architecture MUST:

1. detect systems independently;
2. create one or more `BuildProfile` records;
3. produce per-profile extractor contributions;
4. merge entities only when deterministic equivalence is proven;
5. keep entities separate when equivalence is uncertain.

Do not use “first detected marker” as the semantic reason two systems are related.

`primary_profile_id` MAY exist only when selected deterministically by an explicit rule/configuration. Otherwise it MAY be omitted.

**Status:** DESIGN DECISION.

---

# 24. Build/Test Artifact Acquisition

MAP_CODE_RIG SHOULD be read-only by default.

It MAY consume already-existing:

- CMake File API replies;
- CTest JSON;
- lockfiles;
- generated metadata;
- build-system manifests;
- other documented machine-readable outputs.

Executing configure/build/test commands is not a core requirement.

If a plugin explicitly supports artifact acquisition by running a command:

- it MUST be opt-in or clearly configured;
- no package installation is allowed;
- network access is not required by core;
- command, arguments, environment subset, and resulting metadata that affect extraction MUST be treated as deterministic inputs.

**Status:** DESIGN DECISION.

**Reason:** RIG determinism is about stable extraction from equivalent repository/configuration/build-artifact state, not about making the entire native build deterministic.

---

# 25. Incremental Update

## 25.1 V1 policy

V1 MAY perform a full recomputation of the RIG.

This is the simplest core that satisfies the objective.

## 25.2 Incremental-friendly architecture

Even in V1, the implementation SHOULD use:

- semantic stable IDs;
- deterministic evidence IDs;
- deterministic edge keys;
- per-plugin contribution boundaries;
- snapshot fingerprints.

These make future incremental replacement possible.

## 25.3 What is NOT claimed

The final blueprint does **not** claim that full incremental invalidation is already solved.

Open questions remain around:

- dependency propagation across build profiles;
- generated metadata invalidation;
- cross-plugin equivalence;
- partial SQLite cache invalidation;
- exact change impact.

**Status:** DESIGN DECISION for V1 full recomputation; OPEN QUESTION for true incremental recomputation.

**Evidence:** formal definition contains incremental properties, but concrete implementation studies do not provide a complete invalidation algorithm; SQLite study is full-replacement, not incremental.

---

# 26. JSON Output / Canonical Schema

Canonical output:

```json
{
  "schema_version": "rig-json/v1",
  "generator": {
    "name": "map_code_rig",
    "version": "..."
  },
  "repo": {
    "name": "...",
    "primary_language": "unknown",
    "languages": [],
    "build_profile_ids": [],
    "snapshot_fingerprint": "sha256:...",
    "evidence_ids": []
  },
  "build": {
    "profiles": [],
    "primary_profile_id": null,
    "evidence_ids": []
  },
  "components": [],
  "aggregators": [],
  "runners": [],
  "tests": [],
  "external_packages": [],
  "package_managers": [],
  "edges": [],
  "evidence": [],
  "diagnostics": [],
  "unresolved_references": []
}
```

## 26.1 Canonical relationship rule

`edges[]` is the authoritative relationship representation.

Compatibility projections MAY appear on nodes:

```text
depends_on_ids
external_packages_ids
test_executable_id
test_components_ids
components_being_tested_ids
args_node_ids
evidence_ids
```

but those projections MUST be derivable from `edges[]` and MUST NOT contradict it.

This resolves the difference between the historical schema's ID-set design and the formal graph definition.

**Status:** DESIGN DECISION.

## 26.2 JSON simplicity

Canonical JSON MUST:

- use IDs for cross references;
- avoid recursive objects;
- use explicit field names;
- avoid aggressive alias tables;
- omit optional nulls when schema policy permits;
- never serialize unordered sets;
- be UTF-8;
- use normalized LF line endings;
- use stable indentation;
- contain no host-specific absolute path.

---

# 27. Validation and Determinism

## 27.1 Structural validation

MUST check:

1. unique node IDs;
2. unique edge IDs;
3. edge endpoints exist;
4. node kinds valid;
5. edge types valid;
6. endpoint type constraints valid;
7. every node has evidence;
8. every edge has evidence;
9. source paths safe and normalized;
10. duplicate semantic edges absent;
11. component build-dependency subgraph acyclic;
12. compatibility projections equal canonical edges.

## 27.2 Semantic diagnostics

SHOULD cover, where applicable:

- missing source file;
- broken dependency;
- missing build output;
- orphan node;
- circular dependency;
- inconsistent language/runtime;
- unresolved test executable;
- invalid test mapping;
- duplicate node name where ambiguity matters;
- evidence inconsistency;
- unsupported construct.

## 27.3 Deterministic serialization

The same:

```text
repository snapshot
+
extraction policy/configuration
+
build/test metadata inputs
+
generator/plugin versions
```

MUST produce byte-for-byte equivalent canonical JSON.

No output difference may depend on:

- execution order;
- dict/hash-map ordering;
- filesystem traversal order;
- process scheduling;
- incidental machine hostname;
- absolute root path;
- timestamps;
- UUID;
- random seed;
- LLM behavior.

Serializer MUST:

1. order top-level keys explicitly;
2. sort node arrays by stable ID;
3. sort edge arrays by stable ID;
4. sort evidence arrays by stable ID;
5. sort all ID arrays lexicographically;
6. normalize repository-relative path separators to `/`;
7. normalize Unicode to NFC;
8. normalize line endings;
9. exclude volatile environment values;
10. normalize command representation.

## 27.4 Snapshot fingerprint

A `snapshot_fingerprint` SHOULD be derived from the normalized set of inputs that actually influenced the RIG:

```text
relative_path
file_hash
file_role
plugin
build_profile
```

Records are sorted before hashing.

Do not use filesystem mtime as authoritative snapshot identity.

**Status:** DESIGN DECISION.

---

# 28. LLM Consumption

Canonical JSON is intended to be directly machine-readable and usable as LLM context.

However, RIG construction and LLM consumption remain separate stages:

```text
Repository
   ↓
Deterministic RIG
   ↓
Canonical JSON
   ↓
Consumer / Retrieval adapter
   ↓
LLM
```

A downstream adapter MAY produce:

- filtered component views;
- dependency subgraphs;
- test-focused views;
- tree-expanded subgraphs;
- source-file/evidence bundles.

LocAgent evidence indicates that compact graph-oriented tree output is useful for LLM navigation. This is a consumer formatting choice, not a second authoritative RIG schema.

The consumer MUST treat RIG IDs and evidence as references, not replace them with inferred natural-language descriptions.

**Status:** CONFIRMED boundary + DESIGN DECISION for downstream formatting.

---

# 29. CLI / Interface

Canonical command:

```text
python create_rig_json.py <project_path> <output_json>
```

Optional flags SHOULD remain minimal:

```text
--overwrite
--verbose
```

### Required behavior

- missing/non-directory project root → fatal;
- unwritable output → fatal;
- successful extraction with zero recognized build facts MAY produce a minimal repository RIG plus diagnostics;
- plugin-local failure SHOULD not automatically fail unrelated extractors;
- canonical JSON commit MUST be atomic.

`--format flat|nested` is not necessary because the canonical output has one authoritative format.

**Status:** DESIGN DECISION.

---

# 30. Error Handling

Errors have three levels:

### ERROR

The output cannot be trusted structurally.

Examples:

- broken endpoint;
- missing required evidence;
- invalid canonical serialization;
- dependency DAG violation.

### WARNING

Some facts could not be extracted but the remaining graph is valid.

Examples:

- missing optional metadata;
- unsupported build construct;
- failed optional command acquisition.

### INFO

Diagnostic trace of normal extraction decisions.

All diagnostics MUST be deterministic.

No diagnostic may include secrets.

---

# 31. Performance and Large Repositories

The core optimization principle is **do not scan or model information that RIG does not need**.

V1 SHOULD:

- discover metadata selectively;
- avoid full source traversal;
- parse each relevant build artifact once where practical;
- deduplicate by canonical identity;
- use bounded evidence text;
- avoid embedding/vector generation;
- avoid storing full source content in canonical RIG.

There is no fixed “X files” performance guarantee in this blueprint.

For very large repositories:

1. keep canonical RIG focused on build/test architecture;
2. let downstream LocAgent perform code-level indexing;
3. use optional SQLite/cache for persistence/regression;
4. treat true incremental invalidation as future work.

**Status:** DESIGN DECISION.

---

# 32. SQLite / Persistence

SQLite is **optional persistence/cache**, not the canonical public output.

Study evidence confirms an existing SQLite layer that:

- stores one RIG per database;
- performs full replacement on save;
- uses transactions;
- maps string domain IDs to integer DB IDs.

Final architecture therefore uses SQLite only for:

- durable local snapshots;
- regression comparison;
- optional cache;
- future extraction acceleration.

It MUST NOT become:

- authoritative query semantics;
- a second graph schema;
- a retrieval engine;
- a partial-update engine in V1.

Schema migration/versioning for long-lived SQLite stores remains future work.

**Status:** DESIGN DECISION.

---

# 33. Security / Workspace Boundary

The tool MUST treat the repository root as the analysis boundary.

Canonical rules:

- no absolute host paths in output;
- no `..` path escape;
- symlink traversal MUST NOT silently escape the workspace;
- no package installation;
- no network requirement for core extraction;
- subprocess execution only under explicit plugin policy;
- command/environment secrets MUST be redacted;
- `.env`, credential files, private keys, and similar secret material MUST NOT be copied into evidence snippets.

`.gitignore` MAY be used as one discovery filter, but it must not be treated as a universal semantic rule because build metadata required by RIG can legitimately be ignored by VCS.

**Status:** DESIGN DECISION.

---

# 34. Testing Strategy for the Architecture

Even though this blueprint is not an implementation plan, the architecture is incomplete without testable acceptance criteria.

Required fixture categories:

1. minimal repository;
2. CMake project with libraries/executables;
3. CMake project with CTest tests;
4. npm workspace with lockfile;
5. Maven multi-module;
6. Cargo workspace;
7. Go module;
8. Meson project;
9. multiple build systems in one repository;
10. malformed/incomplete build metadata;
11. unresolved target/reference;
12. duplicate semantic declaration;
13. generated output;
14. repository containing `node_modules`;
15. same repository analyzed through different traversal order.

Determinism test:

```text
same normalized inputs
→ run RIG construction repeatedly
→ canonical JSON bytes MUST match
```

Round-trip tests:

```text
RIG
→ JSON
→ RIG
→ canonicalize
→ compare
```

Optional SQLite test:

```text
RIG
→ SQLite
→ load
→ JSON
→ canonicalize
→ compare
```

---

# 35. Future Roadmap

Future capabilities, not V1 core:

### Phase A — extractor breadth

- additional build systems;
- richer package manager semantics;
- generated metadata adapters.

### Phase B — incremental extraction

- exact input dependency graph;
- invalidation;
- per-profile recomputation;
- cache verification.

### Phase C — retrieval adapter

Separate downstream adapter for:

- filtering;
- traversal;
- compact tree rendering;
- evidence retrieval.

### Phase D — AST/symbol graph

Separate LocAgent-style graph/index:

- directory;
- file;
- class;
- function;
- symbol;
- `contain`;
- `import`;
- `invoke`;
- `inherit`.

### Phase E — richer formal relations

Activate `includes` and `links` only once semantics are grounded well enough in deterministic build evidence.

### Phase F — persistent schema evolution

- SQLite versioning;
- migrations;
- cache compatibility validation.

---

# 36. Open Questions

## OQ-1 — True incremental invalidation

Bagaimana menentukan minimal affected subgraph ketika:

- satu build file berubah;
- generated build metadata berubah;
- one profile changes;
- cross-profile equivalence changes?

**Status:** OPEN QUESTION.

## OQ-2 — Semantic identity under target rename

Sejauh mana ID harus mengikuti logical target identity versus build-system target name ketika target di-rename tetapi artifact semantics tetap sama?

**Status:** OPEN QUESTION.

## OQ-3 — Cross-build equivalence

Kapan npm/CMake/Maven/etc. entities pada monorepo boleh dinyatakan sebagai entity yang sama?

**Status:** OPEN QUESTION.

## OQ-4 — External tool version sensitivity

Kapan version/configuration sebuah external build tool harus dimasukkan ke generator fingerprint?

**Status:** OPEN QUESTION.

## OQ-5 — `includes` / `links`

Bagaimana semantics lintas build systems harus dinormalisasi tanpa mengubah RIG menjadi code graph generik?

**Status:** OPEN QUESTION.

## OQ-6 — Evidence from generated files

Seberapa jauh generated metadata boleh menjadi authoritative ketika file generated berada di luar repository source tree?

**Status:** OPEN QUESTION.

## OQ-7 — Consumer-specific views

Apakah downstream consumer memerlukan one-size-fits-all JSON atau beberapa projection yang tetap berasal dari canonical RIG?

**Status:** OPEN QUESTION.

---

# 37. Architectural Decisions and Rationale

Bagian ini merekonsiliasi perbedaan proposal secara eksplisit. Keputusan akhir di bawah bukan voting antar-LLM.

## AD-01 — Identity

### Decision:

**Status:** DESIGN DECISION

**Decision:** gunakan semantic `identity_key` + cryptographic hash untuk ID semua entity dan edge; buang counter-based IDs.

**Reason:** schema study memang menggunakan counters, tetapi counter bertentangan dengan requirement reproducibility/stable identity. Formal study tidak memberikan algorithm ID yang lengkap, sehingga deterministic semantic IDs harus ditetapkan sebagai desain final.

**Evidence:** `schemas_study.txt` menunjukkan enam global counters; RIG/definition studies menekankan deterministic comparison dan identity concepts.

**Blueprint divergence:**
- DeepSeek: SHA-256 dari type/path/context.
- Gemini: SHA-256 dari node type + canonical path/name.
- ChatGPT: semantic identity key yang lebih eksplisit dan juga stable edge/evidence IDs.
- **Final:** pendekatan semantic identity key + hash karena paling mampu mempertahankan identity tanpa bergantung pada parsing order dan tidak memaksa full content hash.

---

## AD-02 — First-class Edge

### Decision:

**Status:** DESIGN DECISION

**Decision:** `edges[]` adalah authoritative relationship model. `*_ids` hanya projection compatibility.

**Reason:** formal RIG mendefinisikan graph sebagai `G=(N,E)` dan study meminta edge identity/type sebagai first-class concern. Historical implementation memakai ID sets, tetapi itu tidak cukup untuk role, qualifier, evidence, atau edge identity.

**Evidence:** formal RIG study; `rig_study.txt`; RIG paper graph architecture.

**Blueprint divergence:**
- DeepSeek: ID-set relationships sebagai canonical.
- Gemini: property-based ID relations.
- ChatGPT: first-class edge objects.
- **Final:** canonical first-class edges, dengan ID-set projections untuk compatibility.

---

## AD-03 — ExternalPackage / PackageManager

### Decision:

**Status:** DESIGN DECISION

**Decision:** ExternalPackage dan PackageManager dipisahkan sebagai semantic entities; hanya core build/test nodes yang masuk active RIGNode partition.

**Reason:** paper dan schema sama-sama membutuhkan external packages/package managers, sementara formal definition ambigu tentang bagaimana E_external memenuhi `E ⊆ N×N`.

**Evidence:** RIG paper Table I, schema study, formal definition ambiguity analysis.

**Blueprint divergence:**
- DeepSeek dan ChatGPT memisahkan satellite semantic entities.
- Gemini cenderung memperlakukan mereka sebagai entities yang lebih langsung.
- **Final:** separation eksplisit tanpa memaksa ExternalPackage menjadi `Component` atau memperluas active build-node partition secara diam-diam.

---

## AD-04 — AST

### Decision:

**Status:** CONFIRMED

**Decision:** tidak ada AST/symbol graph di core RIG.

**Reason:** RIG paper menetapkan abstraction boundary build/test; LocAgent memiliki code-level graph terpisah.

**Evidence:** RIG paper abstraction boundary; LocAgent directory/file/class/function graph dan tree-sitter.

**Blueprint divergence:** ketiganya umumnya menunda AST; ChatGPT paling eksplisit memisahkannya sebagai layer LocAgent/future. Final mempertahankan pemisahan tersebut.

---

## AD-05 — Parsing source priority

### Decision:

**Status:** DESIGN DECISION

**Decision:** machine-readable build/test metadata > structured manifest/lockfile > source build-file fallback > UNKNOWN.

**Reason:** SPADE/CMake study secara eksplisit mengandalkan File API/CTest, sedangkan npm study menunjukkan ketergantungan pada installed environment dapat membuat hasil tidak hermetic.

**Blueprint divergence:**
- DeepSeek dan ChatGPT: metadata-first.
- Gemini: parser/AST/regex emphasis.
- **Final:** metadata-first; source parsing hanya fallback/evidence, bukan semantic reimplementation.

---

## AD-06 — npm dependency extraction

### Decision:

**Status:** DESIGN DECISION

**Decision:** declaration/lockfile evidence adalah default; `npm list` bukan authoritative default.

**Reason:** npm study memperlihatkan dependency extraction bergantung pada installed state dan dapat menghasilkan repository contamination.

**Evidence:** `npm_entrypoint_study.txt`.

**Blueprint divergence:**
- DeepSeek: lockfile-centric extractor.
- Gemini: package.json/lockfile.
- ChatGPT: explicit correction terhadap npm installed-state behavior.
- **Final:** semua sumber deklaratif yang tersedia diprioritaskan; installed-state command hanya opt-in input.

---

## AD-07 — SQLite

### Decision:

**Status:** DESIGN DECISION

**Decision:** SQLite optional persistence/cache, bukan mandatory core output dan bukan retrieval engine.

**Reason:** study membuktikan SQLite persistence ada, tetapi storage melakukan full replacement dan tidak menyediakan query/traversal/migration.

**Blueprint divergence:**
- DeepSeek: optional SQLite.
- Gemini: V1 tanpa SQLite.
- ChatGPT: optional SQLite cache/store.
- **Final:** optional persistence agar faithful terhadap study tanpa membuat V1 bergantung pada storage subsystem.

---

## AD-08 — Error policy

### Decision:

**Status:** DESIGN DECISION

**Decision:** strict global validation + graceful plugin-local degradation.

**Reason:** deterministic extraction harus menjaga graph integrity, tetapi malformed/incomplete source tidak seharusnya membuat repository yang sebagian valid menjadi sepenuhnya hilang.

**Blueprint divergence:**
- DeepSeek lebih fail-fast.
- Gemini lebih graceful.
- ChatGPT memisahkan ERROR/WARNING dan output commit barrier.
- **Final:** invalid input/global invariant failure = fatal; plugin-local failure = diagnostic + partial graph bila graph tetap valid.

---

## AD-09 — Discovery traversal

### Decision:

**Status:** DESIGN DECISION

**Decision:** targeted metadata discovery, bukan unconditional full-tree traversal.

**Reason:** full traversal meningkatkan risiko vendor/dependency/generated contamination dan tidak dibutuhkan untuk build/test RIG.

**Blueprint divergence:**
- Gemini: sorted broad traversal.
- DeepSeek/ChatGPT: selective discovery.
- **Final:** selective discovery.

---

## AD-10 — Multi-build repositories

### Decision:

**Status:** DESIGN DECISION

**Decision:** independent build profiles; merge hanya bila equivalence deterministically proven.

**Reason:** repository dapat mengandung beberapa build systems yang benar-benar berbeda.

**Blueprint divergence:**
- DeepSeek memberi primary build system berdasarkan detection priority.
- Gemini menempatkan multiple systems pada metadata global.
- ChatGPT memakai build profiles dan conservative merge.
- **Final:** build profiles tanpa arbitrary first-detected semantics.

---

## AD-11 — Incremental update

### Decision:

**Status:** DESIGN DECISION + OPEN QUESTION

**Decision:** V1 full recomputation; internal model incremental-friendly.

**Reason:** study mengakui incremental properties tetapi concrete storage/implementation tidak menyediakan invalidation algorithm.

**Blueprint divergence:**
- DeepSeek mainly discusses extensibility.
- Gemini leaves incremental broad.
- ChatGPT explicitly proposes incremental-friendly stable IDs/fingerprints.
- **Final:** stable semantic identity/fingerprint now; exact incremental invalidation later.

---

## AD-12 — LLM boundary

### Decision:

**Status:** CONFIRMED + DESIGN DECISION

**Decision:** LLM is consumer/investigator, never authoritative graph constructor.

**Reason:** LocAgent is downstream code localization; deterministic knowledgebase track exists specifically after historical LLM-based generation.

**Blueprint divergence:** ketiganya menolak LLM sebagai core constructor; final makes the boundary explicit and permanent.

---

## AD-13 — JSON shape

### Decision:

**Status:** DESIGN DECISION

**Decision:** one flat canonical JSON with first-class `edges`, `evidence`, `diagnostics`, and `unresolved_references`.

**Reason:** RIG paper uses flat identifier-based JSON; first-class edges are required for canonical graph semantics; unresolved uncertainty must be machine-readable.

**Blueprint divergence:**
- DeepSeek/Gemini: flat JSON centered on node relationship ID fields.
- ChatGPT: flat JSON with edge table.
- **Final:** flat JSON with authoritative edges + compatibility projections.

---

# 38. Final Architecture

The final architecture is:

```text
                 repository root
                       │
                       ▼
              ┌─────────────────┐
              │ Repository       │
              │ Discovery        │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Build/Test      │
              │ Profile Detect  │
              └────────┬────────┘
                       │
          ┌────────────┴─────────────┐
          │                          │
          ▼                          ▼
  machine-readable             manifest / lock
  build/test metadata           / source fallback
          │                          │
          └────────────┬─────────────┘
                       ▼
              ┌─────────────────┐
              │ Deterministic   │
              │ Extractor       │
              │ Plugins         │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │ Raw Facts +     │
              │ Evidence        │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │ Canonical       │
              │ Normalizer      │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │ Stable Identity │
              │ Materializer    │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │ Graph Assembly  │
              │ Nodes + Edges   │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │ Validation      │
              └──────┬─────┬────┘
                     │     │
                  ERROR   valid/partial
                     │     │
              no commit    ▼
                           │
                  ┌───────────────┐
                  │ Canonical     │
                  │ JSON          │
                  └──────┬────────┘
                         │
             ┌───────────┴───────────┐
             │                       │
             ▼                       ▼
        LLM / consumer          Optional SQLite
        / LocAgent              persistence/cache
```

This is the single final architecture. No second competing graph architecture is defined inside the blueprint.

---

# 39. Architectural Acceptance Criteria

The implementation is architecturally compliant only when all of the following hold:

1. same normalized inputs produce identical canonical JSON bytes;
2. no node depends on creation order;
3. no edge depends on creation order;
4. every emitted node has evidence;
5. every emitted edge has evidence;
6. component build dependency graph is acyclic;
7. all edge endpoints resolve;
8. repository-relative paths are normalized;
9. absolute host paths are absent from canonical output;
10. unresolved references are explicit;
11. malformed/incomplete inputs do not cause fabricated facts;
12. LLM is absent from authoritative graph construction;
13. AST/symbol graph remains outside core RIG;
14. LocAgent can consume RIG without changing RIG semantics;
15. SQLite, when enabled, round-trips without changing canonical meaning;
16. multiple build systems remain separate unless deterministic equivalence is established;
17. canonical JSON contains one authoritative relationship representation (`edges[]`);
18. legacy `*_ids` fields, when emitted, are projections only;
19. no extractor treats installed dependency state as repository truth unless explicitly declared as an input;
20. V1 does not require a true incremental invalidation engine.

---

# 40. Study Evidence Map

The final architecture was grounded primarily in:

- **RIG paper — `2601.10112v1.txt`**: purpose, abstraction boundary, deterministic extraction, build/test artifact model, schema/extractor separation, JSON view, SQLite usage, CMake evidence sources.
- **RIG formal definition — `rig_definition_study.txt`**: `G=(N,E)`, node/edge partition, acyclicity, evidence completeness, incremental/context properties, and formal ambiguities.
- **Schema study — `schemas_study.txt`**: concrete Pydantic model inventory, node hierarchy, ID counters, evidence validator, legacy relationship representation, RIGPromptData.
- **RIG core study — `rig_study.txt`**: registries, hydration, current ID-set representation, prompt JSON, deterministic comparison, SQLite bridge, evidence-only analysis.
- **SQLite study — `rig_store_study.txt`**: one-RIG-per-database persistence, transactional full replacement, string↔integer ID mapping, lack of query/migration/incremental semantics.
- **Knowledgebase study — `knowledgebase_study.txt`**: six deterministic entrypoints, CMake File API/CTest extraction, runtime/language evidence constraints, validation/failure modes.
- **npm study — `npm_entrypoint_study.txt`**: installed-state dependency extraction and repository contamination risks.
- **LocAgent — `2025.acl-long.426.txt`**: code-level graph, tree-sitter AST, retrieval tools, and strict boundary between code graph and RIG.
- **LLM0 study — `llm0_phases_detail_study.txt`**: historical LLM-based generation, treated as historical context rather than construction specification.

The final blueprint intentionally does not treat unsupported or ambiguous study claims as settled facts.
