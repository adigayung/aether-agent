"""
Deterministic identity utilities for RIG.

Per blueprint sections 7, 14.6, 16.

Rules:
- IDs MUST be derived from semantic identity key + namespace
- Identity key MUST contain only fields that define the logical entity
- No dependency on creation order, counters, UUID, timestamps, or traversal order
- Evidence and edge IDs are deterministic too
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional


def sha256(content: str) -> str:
    """SHA-256 hex digest."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def make_id(namespace: str, identity_key: str) -> str:
    """Produce deterministic ID: <namespace>:<sha256(identity_key)>."""
    return f"{namespace}:{sha256(identity_key)}"


# ── Namespace constants ──────────────────────────────────────────────────

NS_REPO = "repo"
NS_BUILD = "build"
NS_COMPONENT = "component"
NS_AGGREGATOR = "aggregator"
NS_RUNNER = "runner"
NS_TEST = "test"
NS_EXTERNAL_PACKAGE = "external_package"
NS_PACKAGE_MANAGER = "package_manager"
NS_EDGE = "edge"
NS_EVIDENCE = "evidence"
NS_UNRESOLVED = "unresolved"


# ── Canonical path normalization ────────────────────────────────────────

def normalize_path(path: str) -> str:
    """Normalize a repository-relative path to POSIX / with no .. escapes.

    Per blueprint section 8 (source files) and 33 (security/workspace).
    """
    # Convert backslashes to forward slashes
    normalized = path.replace("\\", "/")
    # Resolve .. components (but ensure we stay relative)
    parts = []
    for part in normalized.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part == "." or part == "":
            continue
        else:
            parts.append(part)
    return "/".join(parts)


def normalize_command(command: str) -> str:
    """Normalize a command string for deterministic identity."""
    # Normalize whitespace, strip, lowercase
    return " ".join(command.strip().lower().split())


# ── Component identity ───────────────────────────────────────────────────

def component_identity_key(
    profile_key: str,
    target_name: str,
    scope: str = "",
    language: str = "unknown",
) -> str:
    """Canonical identity key for a Component.

    Pattern: component|<plugin>|profile=<profile-key>|target=<target-name>|scope=<scope>
    """
    parts = [
        "component",
        f"profile={profile_key}",
        f"target={target_name}",
    ]
    if scope:
        parts.append(f"scope={scope}")
    if language and language != "unknown":
        parts.append(f"language={language}")
    return "|".join(parts)


def component_id(profile_key: str, target_name: str,
                 scope: str = "", language: str = "unknown") -> str:
    return make_id(NS_COMPONENT,
                   component_identity_key(profile_key, target_name, scope, language))


# ── Aggregator identity ──────────────────────────────────────────────────

def aggregator_identity_key(
    plugin: str,
    workspace: str,
    name: str,
) -> str:
    """Canonical identity key for an Aggregator."""
    parts = [
        "aggregator",
        f"plugin={plugin}",
        f"workspace={workspace}",
        f"name={name}",
    ]
    return "|".join(parts)


def aggregator_id(plugin: str, workspace: str, name: str) -> str:
    return make_id(NS_AGGREGATOR, aggregator_identity_key(plugin, workspace, name))


# ── Runner identity ──────────────────────────────────────────────────────

def runner_identity_key(
    plugin: str,
    profile_key: str,
    command: str,
    owner_id: str = "",
) -> str:
    """Canonical identity key for a Runner."""
    parts = [
        "runner",
        f"plugin={plugin}",
        f"profile={profile_key}",
        f"command={normalize_command(command)}",
    ]
    if owner_id:
        parts.append(f"owner={owner_id}")
    return "|".join(parts)


def runner_id(plugin: str, profile_key: str,
              command: str, owner_id: str = "") -> str:
    return make_id(NS_RUNNER, runner_identity_key(plugin, profile_key, command, owner_id))


# ── Test identity ────────────────────────────────────────────────────────

def test_identity_key(
    plugin: str,
    profile_key: str,
    test_name: str,
    command: str = "",
    scope: str = "",
) -> str:
    """Canonical identity key for a TestDefinition."""
    parts = [
        "test",
        f"plugin={plugin}",
        f"profile={profile_key}",
        f"name={test_name}",
    ]
    if command:
        parts.append(f"command={normalize_command(command)}")
    if scope:
        parts.append(f"scope={scope}")
    return "|".join(parts)


def test_id(plugin: str, profile_key: str, test_name: str,
            command: str = "", scope: str = "") -> str:
    return make_id(NS_TEST, test_identity_key(plugin, profile_key, test_name, command, scope))


# ── External package identity ────────────────────────────────────────────

def external_package_identity_key(
    ecosystem: str,
    coordinate: str,
    scope: str = "runtime",
) -> str:
    """Canonical identity key for an ExternalPackage."""
    parts = [
        "external_package",
        f"ecosystem={ecosystem}",
        f"coordinate={coordinate}",
        f"scope={scope}",
    ]
    return "|".join(parts)


def external_package_id(ecosystem: str, coordinate: str,
                        scope: str = "runtime") -> str:
    return make_id(NS_EXTERNAL_PACKAGE,
                   external_package_identity_key(ecosystem, coordinate, scope))


# ── Package manager identity ─────────────────────────────────────────────

def package_manager_identity_key(ecosystem: str, name: str) -> str:
    """Canonical identity key for a PackageManager."""
    return f"package_manager|ecosystem={ecosystem}|name={name}"


def package_manager_id(ecosystem: str, name: str) -> str:
    return make_id(NS_PACKAGE_MANAGER,
                   package_manager_identity_key(ecosystem, name))


# ── Evidence identity ────────────────────────────────────────────────────

def evidence_identity_key(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    call_stack: Optional[str] = None,
) -> str:
    """Canonical identity key for Evidence."""
    parts = [normalize_path(file_path)]
    if start_line is not None:
        parts.append(f"L{start_line}")
    if end_line is not None:
        parts.append(f"L{end_line}")
    if call_stack:
        # Use a stable hash of call stack to keep key bounded
        parts.append(sha256(call_stack)[:16])
    return "|".join(parts)


def evidence_id(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    call_stack: Optional[str] = None,
) -> str:
    return make_id(NS_EVIDENCE,
                   evidence_identity_key(file_path, start_line, end_line, call_stack))


# ── Edge identity ────────────────────────────────────────────────────────

def edge_identity_key(
    edge_type: str,
    source_id: str,
    target_id: str,
    role: Optional[str] = None,
    qualifier: Optional[str] = None,
) -> str:
    """Canonical edge key: (type, source, target, role, qualifier)."""
    parts = [edge_type, source_id, target_id]
    if role:
        parts.append(role)
    if qualifier:
        parts.append(qualifier)
    return "|".join(parts)


def edge_id(
    edge_type: str,
    source_id: str,
    target_id: str,
    role: Optional[str] = None,
    qualifier: Optional[str] = None,
) -> str:
    return make_id(NS_EDGE,
                   edge_identity_key(edge_type, source_id, target_id, role, qualifier))


# ── Code entity identity ─────────────────────────────────────────────────

NS_CODE_FILE = "code_file"
NS_CODE_MODULE = "code_module"
NS_CODE_CLASS = "code_class"
NS_CODE_FUNCTION = "code_function"
NS_CODE_SYMBOL = "code_symbol"


def code_file_identity_key(file_path: str, language: str = "python") -> str:
    """Canonical identity key for a source file.

    Pattern: code_file|<language>|<normalized_path>
    """
    return f"code_file|{language}|{normalize_path(file_path)}"


def code_file_id(file_path: str, language: str = "python") -> str:
    return make_id(NS_CODE_FILE, code_file_identity_key(file_path, language))


def code_module_identity_key(file_path: str) -> str:
    """Canonical identity key for a code module.

    Pattern: code_module|<normalized_path>
    """
    return f"code_module|{normalize_path(file_path)}"


def code_module_id(file_path: str) -> str:
    return make_id(NS_CODE_MODULE, code_module_identity_key(file_path))


def code_class_identity_key(file_path: str, class_name: str) -> str:
    """Canonical identity key for a class.

    Pattern: code_class|<normalized_path>|<class_name>
    """
    return f"code_class|{normalize_path(file_path)}|{class_name}"


def code_class_id(file_path: str, class_name: str) -> str:
    return make_id(NS_CODE_CLASS, code_class_identity_key(file_path, class_name))


def code_function_identity_key(file_path: str, function_name: str,
                               parent_class: str = "") -> str:
    """Canonical identity key for a function or method.

    Pattern: code_function|<path>|<name>|parent=<parent_class>
    """
    parts = [f"code_function|{normalize_path(file_path)}|{function_name}"]
    if parent_class:
        parts.append(f"parent={parent_class}")
    return "|".join(parts)


def code_function_id(file_path: str, function_name: str,
                     parent_class: str = "") -> str:
    return make_id(NS_CODE_FUNCTION,
                   code_function_identity_key(file_path, function_name, parent_class))


def code_symbol_identity_key(file_path: str, name: str,
                             symbol_type: str = "unknown",
                             line: int = 0) -> str:
    """Canonical identity key for a symbol.

    Pattern: code_symbol|<path>|<name>|<type>|L<line>
    """
    return f"code_symbol|{normalize_path(file_path)}|{name}|{symbol_type}|L{line}"


def code_symbol_id(file_path: str, name: str,
                   symbol_type: str = "unknown",
                   line: int = 0) -> str:
    return make_id(NS_CODE_SYMBOL,
                   code_symbol_identity_key(file_path, name, symbol_type, line))

def unresolved_id(source_entity_id: str, reference_text: str,
                  reference_kind: str = "other") -> str:
    key = f"unresolved|source={source_entity_id}|ref={reference_text}|kind={reference_kind}"
    return make_id(NS_UNRESOLVED, key)


# ── Build profile identity ───────────────────────────────────────────────

def build_profile_identity_key(build_system: str, scope: str = "") -> str:
    """Canonical identity key for a BuildProfile."""
    return f"build|{build_system}|scope={scope}" if scope else f"build|{build_system}"


def build_profile_id(build_system: str, scope: str = "") -> str:
    return make_id(NS_BUILD, build_profile_identity_key(build_system, scope))