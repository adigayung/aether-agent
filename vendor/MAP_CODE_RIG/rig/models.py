"""
Core data models for Repository Intelligence Graph (RIG).

Follows FINAL_OPENAI_RIG_BLUEPRINT.md sections 6-14.

Node universe:
  Core build/test nodes: component, aggregator, runner, test
  Satellite entities: external_package, package_manager
  Other: evidence (not a graph node but linked by edges)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Identity helpers ──────────────────────────────────────────────────────

def _stable_hash(identity_key: str) -> str:
    """SHA-256 hex digest of a canonical identity key."""
    return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()


def make_id(namespace: str, identity_key: str) -> str:
    """Produce deterministic ID: <namespace>:<sha256(identity_key)>."""
    return f"{namespace}:{_stable_hash(identity_key)}"


# ── Enums ─────────────────────────────────────────────────────────────────

class ComponentType:
    EXECUTABLE = "executable"
    SHARED_LIBRARY = "shared_library"
    STATIC_LIBRARY = "static_library"
    PACKAGE_LIBRARY = "package_library"
    VM = "vm"
    INTERPRETED = "interpreted"
    UNKNOWN = "unknown"

    ALL = {EXECUTABLE, SHARED_LIBRARY, STATIC_LIBRARY, PACKAGE_LIBRARY, VM, INTERPRETED, UNKNOWN}


class EdgeType:
    DEPENDS_ON = "depends_on"
    TESTS = "tests"
    INCLUDES = "includes"
    LINKS = "links"
    EXTERNAL = "external"
    # Code-level relationships
    CONTAINS = "contains"
    IMPORTS = "imports"
    INVOKES = "invokes"
    INHERITS = "inherits"

    ALL = {DEPENDS_ON, TESTS, INCLUDES, LINKS, EXTERNAL, CONTAINS, IMPORTS, INVOKES, INHERITS}
    MVP = {DEPENDS_ON, TESTS, EXTERNAL, CONTAINS, IMPORTS, INVOKES, INHERITS}


class DependsOnRole:
    BUILD = "build"
    ORCHESTRATION = "orchestration"
    RUNNER_INPUT = "runner_input"
    TEST_SUPPORT = "test_support"

    ALL = {BUILD, ORCHESTRATION, RUNNER_INPUT, TEST_SUPPORT}


class TestsRole:
    SUBJECT = "subject"
    HARNESS = "harness"
    EXECUTABLE = "executable"

    ALL = {SUBJECT, HARNESS, EXECUTABLE}


class TestKind:
    UNIT = "unit"
    INTEGRATION = "integration"
    SYSTEM = "system"
    E2E = "e2e"
    PERFORMANCE = "performance"
    SMOKE = "smoke"
    COMPILER = "compiler"
    UNKNOWN = "unknown"

    ALL = {UNIT, INTEGRATION, SYSTEM, E2E, PERFORMANCE, SMOKE, COMPILER, UNKNOWN}


class EvidenceLocatorKind:
    FILE = "file"
    BUILD_ARTIFACT = "build_artifact"
    MANIFEST = "manifest"
    LOCKFILE = "lockfile"
    GENERATED_METADATA = "generated_metadata"
    CALL_STACK = "call_stack"

    ALL = {FILE, BUILD_ARTIFACT, MANIFEST, LOCKFILE, GENERATED_METADATA, CALL_STACK}


class DiagnosticLevel:
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    ALL = {ERROR, WARNING, INFO}


class UnresolvedKind:
    TARGET = "target"
    PACKAGE = "package"
    TEST = "test"
    FILE = "file"
    OTHER = "other"
    SYMBOL = "symbol"

    ALL = {TARGET, PACKAGE, TEST, FILE, OTHER, SYMBOL}


class CodeNodeKind:
    """Kinds of code-level nodes."""
    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    SYMBOL = "symbol"

    ALL = {FILE, MODULE, CLASS, FUNCTION, METHOD, SYMBOL}


class CodeRelationKind:
    """Kinds of code-level relationships."""
    CONTAINS = "contains"
    IMPORTS = "imports"
    INVOKES = "invokes"
    INHERITS = "inherits"

    ALL = {CONTAINS, IMPORTS, INVOKES, INHERITS}


# ── Core node models ──────────────────────────────────────────────────────

@dataclass
class Component:
    """Buildable artifact or package-level artifact."""
    id: str
    kind: str = "component"
    name: str = ""
    type: str = ComponentType.UNKNOWN
    programming_language: str = "unknown"
    runtime: Optional[str] = None
    source_files: List[str] = field(default_factory=list)
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    build_profile_ids: List[str] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Aggregator:
    """Orchestration target that groups other targets."""
    id: str
    kind: str = "aggregator"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    build_profile_ids: List[str] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Runner:
    """Command invocation target."""
    id: str
    kind: str = "runner"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    build_profile_ids: List[str] = field(default_factory=list)
    command: Optional[str] = None
    arguments: List[str] = field(default_factory=list)
    working_directory: Optional[str] = None
    environment_keys: List[str] = field(default_factory=list)
    owner_node_id: Optional[str] = None
    purpose: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestDefinition:
    """Test definition/invocation (not runtime result)."""
    id: str
    kind: str = "test"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    build_profile_ids: List[str] = field(default_factory=list)
    framework: Optional[str] = None
    test_kind: str = TestKind.UNKNOWN
    source_files: List[str] = field(default_factory=list)
    runner_node_id: Optional[str] = None
    test_executable_node_id: Optional[str] = None
    components_being_tested_ids: List[str] = field(default_factory=list)
    command: Optional[str] = None
    arguments: List[str] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


# ── Satellite entities ────────────────────────────────────────────────────

@dataclass
class ExternalPackage:
    """External dependency from an ecosystem/package manager."""
    id: str
    kind: str = "external_package"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    package_manager_id: Optional[str] = None
    ecosystem: Optional[str] = None
    coordinate: Optional[str] = None
    declared_version: Optional[str] = None
    resolved_version: Optional[str] = None
    dependency_scope: Optional[str] = None
    optional: bool = False
    peer: bool = False
    source: Optional[str] = None
    lockfile_path: Optional[str] = None


@dataclass
class PackageManager:
    """Package manager / ecosystem identifier."""
    id: str
    kind: str = "package_manager"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    ecosystem: Optional[str] = None


# ── Code entity models (source-code mapping) ────────────────────────────

@dataclass
class CodeFile:
    """A source file in the repository."""
    id: str
    kind: str = "file"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    file_path: str = ""
    language: str = "unknown"
    parse_success: bool = True
    parse_error: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeModule:
    """A Python module (a .py file or package)."""
    id: str
    kind: str = "module"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    file_path: str = ""
    is_package: bool = False
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeClass:
    """A class defined in source code."""
    id: str
    kind: str = "class"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    bases: List[str] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeFunction:
    """A function (module-level or method) defined in source code."""
    id: str
    kind: str = "function"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    is_method: bool = False
    parent_class_id: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodeSymbol:
    """A symbol/reference in source code (variable, import target, etc.)."""
    id: str
    kind: str = "symbol"
    name: str = ""
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0
    symbol_type: str = "unknown"
    resolved_target_id: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)


# ── Edge model ────────────────────────────────────────────────────────────

@dataclass
class Edge:
    """First-class directed relationship between two RIG entities.

    Per blueprint section 14, edges[] is the authoritative relationship
    representation. Legacy *_ids fields on nodes are compatibility projections.
    """
    id: str
    type: str
    source: str
    target: str
    evidence_ids: List[str] = field(default_factory=list)
    role: Optional[str] = None
    qualifier: Optional[str] = None
    origin_plugin: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def compute_id(type_: str, source: str, target: str,
                   role: Optional[str] = None,
                   qualifier: Optional[str] = None) -> str:
        """Deterministic edge ID from semantic key."""
        parts = [type_, source, target]
        if role:
            parts.append(role)
        if qualifier:
            parts.append(qualifier)
        key = "|".join(parts)
        return make_id("edge", key)


# ── Evidence model ────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """Evidence reference backing a node or edge.

    Per blueprint section 15, every node and edge MUST have ≥1 evidence.
    """
    id: str
    locator_kind: str = EvidenceLocatorKind.FILE
    file_path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    call_stack: Optional[str] = None
    source_ref: Optional[str] = None
    text_snippet: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def compute_id(file_path: str, start_line: Optional[int] = None,
                   end_line: Optional[int] = None,
                   call_stack: Optional[str] = None) -> str:
        """Deterministic evidence ID."""
        parts = [file_path]
        if start_line is not None:
            parts.append(str(start_line))
        if end_line is not None:
            parts.append(str(end_line))
        if call_stack:
            parts.append(call_stack)
        key = "|".join(parts)
        return make_id("evidence", key)


# ── Package / collection models ───────────────────────────────────────────

@dataclass
class BuildProfile:
    """A build system profile detected in the repository."""
    id: str
    name: str = ""
    build_system: Optional[str] = None
    identity_key: str = ""
    evidence_ids: List[str] = field(default_factory=list)

    @staticmethod
    def compute_id(build_system: str, scope: str = "") -> str:
        key = f"build|{build_system}|{scope}"
        return make_id("build", key)


@dataclass
class Repo:
    """Top-level repository metadata."""
    name: str = ""
    primary_language: str = "unknown"
    languages: List[str] = field(default_factory=list)
    build_profile_ids: List[str] = field(default_factory=list)
    snapshot_fingerprint: Optional[str] = None
    revision: Optional[str] = None
    vcs: Optional[str] = None
    analysis_status: str = "complete"
    evidence_ids: List[str] = field(default_factory=list)


# ── Diagnostic models ────────────────────────────────────────────────────

@dataclass
class Diagnostic:
    """Validation/process diagnostic message."""
    level: str = DiagnosticLevel.INFO
    message: str = ""
    source: Optional[str] = None
    entity_id: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UnresolvedReference:
    """First-class diagnostic fact for unresolved references.

    Per blueprint section 20, unresolved references are diagnostic facts,
    not fake graph edges.
    """
    id: str
    kind: str = "reference"
    source_entity_id: str = ""
    reference_text: str = ""
    reference_kind: str = UnresolvedKind.OTHER
    candidate_ids: List[str] = field(default_factory=list)
    reason: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    status: str = "unresolved"


# ── Generator info ────────────────────────────────────────────────────────

@dataclass
class Generator:
    name: str = "map_code_rig"
    version: str = "1.0.0"


# ── Top-level RIG ─────────────────────────────────────────────────────────

@dataclass
class RIG:
    """Top-level container for a complete Repository Intelligence Graph."""
    schema_version: str = "rig-json/v1"
    generator: Generator = field(default_factory=Generator)

    # Repository metadata
    repo: Repo = field(default_factory=Repo)
    build: Dict[str, Any] = field(default_factory=lambda: {
        "profiles": [],
        "primary_profile_id": None,
        "evidence_ids": []
    })

    # Core node collections
    components: List[Component] = field(default_factory=list)
    aggregators: List[Aggregator] = field(default_factory=list)
    runners: List[Runner] = field(default_factory=list)
    tests: List[TestDefinition] = field(default_factory=list)

    # Code-level entities (source-code mapping)
    code_files: List[CodeFile] = field(default_factory=list)
    code_modules: List[CodeModule] = field(default_factory=list)
    code_classes: List[CodeClass] = field(default_factory=list)
    code_functions: List[CodeFunction] = field(default_factory=list)
    code_symbols: List[CodeSymbol] = field(default_factory=list)

    # Satellite entities
    external_packages: List[ExternalPackage] = field(default_factory=list)
    package_managers: List[PackageManager] = field(default_factory=list)

    # Relationships (authoritative)
    edges: List[Edge] = field(default_factory=list)

    # Evidence
    evidence: List[Evidence] = field(default_factory=list)

    # Diagnostics
    diagnostics: List[Diagnostic] = field(default_factory=list)
    unresolved_references: List[UnresolvedReference] = field(default_factory=list)

    # Internal lookup (not serialized)
    _node_map: Dict[str, Any] = field(default_factory=dict, repr=False)
    _edge_map: Dict[str, Edge] = field(default_factory=dict, repr=False)
    _evidence_map: Dict[str, Evidence] = field(default_factory=dict, repr=False)

    def index_nodes(self):
        """Build internal lookup maps for fast access."""
        self._node_map.clear()
        self._edge_map.clear()
        self._evidence_map.clear()

        for node_list in [self.components, self.aggregators,
                          self.runners, self.tests,
                          self.external_packages, self.package_managers,
                          self.code_files, self.code_modules,
                          self.code_classes, self.code_functions,
                          self.code_symbols]:
            for node in node_list:
                self._node_map[node.id] = node

        for edge in self.edges:
            self._edge_map[edge.id] = edge

        for ev in self.evidence:
            self._evidence_map[ev.id] = ev

    def get_node(self, node_id: str) -> Optional[Any]:
        return self._node_map.get(node_id)

    def get_edge(self, edge_id: str) -> Optional[Edge]:
        return self._edge_map.get(edge_id)

    def get_evidence(self, evidence_id: str) -> Optional[Evidence]:
        return self._evidence_map.get(evidence_id)