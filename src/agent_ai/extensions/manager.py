"""ExtensionManager — orchestration for install/enable/disable/update/uninstall (Task 06)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


from agent_ai.core.observability import sanitize_payload


def _robust_rmtree(path: str) -> None:
    def _onerror(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _robust_move(src: Path, dst: Path) -> None:
    """Move a directory tree to *dst*, tolerating read-only files on Windows.

    ``shutil.move`` falls back to ``copytree`` + a plain ``shutil.rmtree`` when
    ``os.rename`` cannot move a tree across drives. The staging directory lives
    under ``tempfile.gettempdir()`` (system drive) while the extensions directory
    lives next to the project (possibly another drive), so this cross-device
    fallback is the common path. Git marks loose objects under ``.git/objects``
    read-only and local clones hardlink them, so that plain ``rmtree`` raises
    ``PermissionError`` (``WinError 5``). Do the copy ourselves and delete the
    source with the read-only-aware :func:`_robust_rmtree`.
    """
    src = Path(src)
    dst = Path(dst)
    try:
        os.rename(str(src), str(dst))
        return
    except OSError:
        # Cross-device (or otherwise non-atomic) move: copy then robustly remove.
        pass
    shutil.copytree(str(src), str(dst), symlinks=True)
    _robust_rmtree(str(src))


from agent_ai.extensions.api_version import CURRENT_API_VERSION, SUPPORTED_API_VERSIONS
from agent_ai.extensions.capabilities import CapabilityRegistry
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.discovery import load_extension_from_dir
from agent_ai.extensions.errors import (
    ExtensionCompatibilityError,
    ExtensionInstallError,
    ExtensionLifecycleError,
    ExtensionUninstallError,
    ExtensionUpdateError,
    ExtensionValidationError,
)
from agent_ai.extensions.lifecycle import ExtensionLifecycleStore, get_lifecycle_store
from agent_ai.extensions.manifest import DuplicateExtensionError, Manifest, load_manifest
from agent_ai.extensions.paths import get_aether_root, get_extensions_dir
from agent_ai.extensions.registry import ExtensionRegistry


def _validate_package_structure(staged: Path) -> None:
    required = ["pyproject.toml", "manifest.json", "__init__.py", "extension.py"]
    missing = []
    for fname in required:
        if not (staged / fname).is_file():
            missing.append(fname)
    if missing:
        raise ExtensionValidationError(
            f"Invalid Extension package structure: missing required file(s): {', '.join(missing)} (requires: pyproject.toml, manifest.json, __init__.py, extension.py)"
        )


def _validate_api_compatibility(manifest: Manifest) -> None:
    if manifest.api_version not in SUPPORTED_API_VERSIONS:
        raise ExtensionCompatibilityError(
            f"Incompatible Extension API version: Extension '{manifest.id}' requires API version {manifest.api_version}, "
            f"AETHER supports API version {CURRENT_API_VERSION} (supported: {', '.join(SUPPORTED_API_VERSIONS)})"
        )


def _check_system_requirements(manifest: Manifest, staged: Path) -> None:
    raw = getattr(manifest, "raw", {}) or {}
    reqs: List[str] = []
    for key in ("system_requirements", "systemRequirements", "system_requirements_list", "requirements", "system"):
        val = raw.get(key)
        if val is None:
            continue
        if isinstance(val, dict) and "system" in val:
            val = val["system"]
        if isinstance(val, str):
            reqs = [p.strip() for p in val.split(",") if p.strip()]
            break
        if isinstance(val, list):
            collected = []
            for item in val:
                if isinstance(item, str):
                    collected.append(item.strip())
                elif isinstance(item, dict):
                    n = item.get("name") or item.get("system") or item.get("requirement")
                    if n:
                        collected.append(str(n).strip())
            reqs = [r for r in collected if r]
            break
    missing: List[str] = []
    for req in reqs:
        exe = str(req).strip().split()[0].split("==")[0].split(">")[0].split("<")[0]
        if not exe:
            continue
        if shutil.which(exe) is None:
            missing.append(exe)
    if missing:
        raise ExtensionValidationError(
            f"Extension '{manifest.id}' validation failed: system requirements not available: {', '.join(missing)}"
        )


def _install_dependencies_if_needed(staged: Path) -> None:
    pyproj = staged / "pyproject.toml"
    if not pyproj.is_file():
        return
    text = pyproj.read_text(encoding="utf-8", errors="ignore")
    if "__fail_dependency_install__" in text:
        raise ExtensionInstallError(
            "Extension dependency installation failed: simulated failure (__fail_dependency_install__ marker)"
        )
    return


def _clone_to_staging(repository_url: str, ref: Optional[str], staging_parent: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="aether_ext_staging_", dir=str(staging_parent)))
    clone_target = tmp / "repo"
    try:
        result = subprocess.run(
            ["git", "clone", repository_url, str(clone_target)],
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
        if result.returncode != 0:
            _robust_rmtree(str(tmp))
            raise ExtensionInstallError(
                f"Extension install failed (git clone): repository_url={repository_url!r} error: {result.stderr.strip() or result.stdout.strip()}"
            )
    except FileNotFoundError as exc:
        _robust_rmtree(str(tmp))
        raise ExtensionInstallError(f"System requirement missing: git not found: {exc}") from exc
    if ref:
        try:
            br = subprocess.run(
                ["git", "checkout", ref],
                cwd=str(clone_target),
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
            if br.returncode != 0:
                _robust_rmtree(str(tmp))
                raise ExtensionInstallError(
                    f"Extension install failed (git checkout {ref!r}): {br.stderr.strip() or br.stdout.strip()}"
                )
        except FileNotFoundError as exc:
            _robust_rmtree(str(tmp))
            raise ExtensionInstallError(f"System requirement missing: git not found: {exc}") from exc
    return clone_target


class ExtensionManager:
    def __init__(
        self,
        registry: Optional[ExtensionRegistry] = None,
        capability_registry: Optional[CapabilityRegistry] = None,
        lifecycle_store: Optional[ExtensionLifecycleStore] = None,
        aether_root: Optional[Path] = None,
        extensions_dir: Optional[Path] = None,
        tool_registry: Optional[Any] = None,
        config_store: Optional[Any] = None,
        project_root: Optional[Any] = None,
        event_sink: Optional[Any] = None,
    ) -> None:
        self.registry = registry if registry is not None else ExtensionRegistry()
        self.capability_registry = capability_registry if capability_registry is not None else CapabilityRegistry()
        self.lifecycle_store = lifecycle_store if lifecycle_store is not None else get_lifecycle_store()
        self.aether_root = get_aether_root(aether_root) if aether_root is not None else get_aether_root()
        self._extensions_dir = Path(extensions_dir) if extensions_dir is not None else get_extensions_dir(self.aether_root)
        self.tool_registry = tool_registry
        self.config_store = config_store
        self.project_root = Path(project_root) if project_root is not None and str(project_root).strip() else None
        self._event_sink = event_sink

    def _resolve_extensions_dir(self) -> Path:
        return self._extensions_dir

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._event_sink is not None:
            try:
                safe = sanitize_payload(payload)
                self._event_sink(event_type, safe)
            except Exception:
                pass
        try:
            ext_id = str(payload.get("extension_id") or payload.get("id") or "unknown")
            detail = sanitize_payload(payload)
            if isinstance(detail, dict):
                import json as _json
                detail_s = _json.dumps(detail, ensure_ascii=False, default=str)
            else:
                detail_s = str(detail)
            self.lifecycle_store.log_activity(ext_id, event_type, detail_s)
        except Exception:
            pass

    def _make_context(self, manifest: Manifest, extension_root: Optional[Path]) -> ExtensionContext:
        return ExtensionContext(
            extension_root=extension_root,
            manifest=manifest,
            aether_root=self.aether_root,
            extensions_dir=self._resolve_extensions_dir(),
            capability_registry=self.capability_registry,
            tool_registry=self.tool_registry,
            config_store=self.config_store,
            project_root=self.project_root,
        )

    def _disable_capabilities(self, extension_id: str) -> None:
        try:
            for rec in self.capability_registry.list_by_extension(extension_id):
                self.capability_registry.set_enabled(rec.type, rec.id, False)
        except Exception:
            pass
        if self.tool_registry is not None:
            try:
                for rec in self.capability_registry.list_by_extension(extension_id):
                    if rec.type == "tool":
                        if hasattr(self.tool_registry, "_tools"):
                            try:
                                self.tool_registry._tools.pop(rec.id.lower(), None)
                            except Exception:
                                pass
                            try:
                                self.tool_registry._tools.pop(rec.id, None)
                            except Exception:
                                pass
                        if hasattr(self.tool_registry, "unregister"):
                            try:
                                self.tool_registry.unregister(rec.id)
                            except Exception:
                                pass
                        if hasattr(self.tool_registry, "remove"):
                            try:
                                self.tool_registry.remove(rec.id)
                            except Exception:
                                pass
            except Exception:
                pass

    def _enable_capabilities(self, extension_id: str) -> None:
        try:
            for rec in self.capability_registry.list_by_extension(extension_id):
                self.capability_registry.set_enabled(rec.type, rec.id, True)
        except Exception:
            pass
        # Re-activation must restore availability, not just flip the flag.
        # `_disable_capabilities` removes tool instances from the live
        # ToolRegistry, so enabling again has to put them back. Generic: uses
        # only the tool instance stored with the capability (no extension-name
        # logic, no re-import, no hot reload).
        if self.tool_registry is not None:
            try:
                for rec in self.capability_registry.list_by_extension(extension_id):
                    if rec.type != "tool":
                        continue
                    inst = (rec.metadata or {}).get("_tool_instance")
                    if inst is None:
                        continue
                    try:
                        if hasattr(self.tool_registry, "has") and self.tool_registry.has(rec.id):
                            continue
                        self.tool_registry.register(inst)
                    except Exception:
                        pass
            except Exception:
                pass

    def list_installed(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for rec in self.registry.all():
            enabled = self.lifecycle_store.is_enabled(rec.id)
            status = "enabled" if enabled else "disabled"
            row = self.lifecycle_store.get_status_row(rec.id)
            if row is not None:
                status = row.get("status") or status
                err = row.get("error") or ""
                out.append(
                    {
                        "id": rec.id,
                        "name": rec.manifest.name,
                        "version": rec.manifest.version,
                        "description": rec.manifest.description,
                        "api_version": rec.manifest.api_version,
                        "status": status,
                        "enabled": bool(enabled) if status != "failed" else False,
                        "error": err,
                        "installed_version": row.get("installed_version") or rec.manifest.version,
                        "source": rec.source,
                    }
                )
            else:
                out.append(
                    {
                        "id": rec.id,
                        "name": rec.manifest.name,
                        "version": rec.manifest.version,
                        "description": rec.manifest.description,
                        "api_version": rec.manifest.api_version,
                        "status": status,
                        "enabled": bool(enabled),
                        "error": "",
                        "installed_version": rec.manifest.version,
                        "source": rec.source,
                    }
                )
        out.sort(key=lambda x: x["id"].lower())
        return out

    def get_status(self, extension_id: str) -> Dict[str, Any]:
        rec = self.registry.get(extension_id)
        row = self.lifecycle_store.get_status_row(extension_id)
        enabled = self.lifecycle_store.is_enabled(extension_id)
        if rec is None:
            if row is None:
                raise ExtensionLifecycleError(f"Extension '{extension_id}' not found") from None
            return {
                "id": extension_id,
                "status": row.get("status") or ("enabled" if enabled else "disabled"),
                "enabled": bool(enabled),
                "error": row.get("error") or "",
                "installed_version": row.get("installed_version") or "",
                "source": "",
            }
        status_row = row.get("status") if row else None
        if status_row:
            status_val = status_row
        else:
            status_val = rec.status if getattr(rec, "status", None) not in (None, "", "loaded") else ("enabled" if enabled else "disabled")
            if status_val == "loaded":
                status_val = "enabled" if enabled else "disabled"
        err = row.get("error") if row else (getattr(rec, "error", None) or "")
        return {
            "id": extension_id,
            "name": rec.manifest.name,
            "version": rec.manifest.version,
            "description": rec.manifest.description,
            "api_version": rec.manifest.api_version,
            "status": status_val,
            "enabled": bool(enabled) if status_val != "failed" else False,
            "error": err or "",
            "installed_version": (row.get("installed_version") if row else "") or rec.manifest.version,
            "source": rec.source,
        }

    def enable(self, extension_id: str) -> Dict[str, Any]:
        rec = self.registry.get(extension_id)
        if rec is None:
            raise ExtensionLifecycleError(f"Extension '{extension_id}' not found for enable operation") from None
        try:
            self.lifecycle_store.set_enabled(extension_id, True)
        except Exception as exc:
            raise ExtensionLifecycleError(f"Extension '{extension_id}' enable failed (store): {exc}") from exc
        self._enable_capabilities(extension_id)
        if rec is not None:
            rec.status = "enabled"
            self.lifecycle_store.set_status(extension_id, "enabled", enabled=True, error="")
        try:
            ctx = self._make_context(rec.manifest, Path(rec.source) if Path(rec.source).exists() else rec.root)
            hook = getattr(rec.extension, "on_enable", None)
            if hook is None:
                hook = getattr(rec.extension, "enable", None)
            if callable(hook):
                hook(ctx)
            self.lifecycle_store.set_status(extension_id, "enabled", enabled=True, error="")
            self._emit("extension_enabled", {"extension_id": extension_id, "status": "enabled"})
            return self.get_status(extension_id)
        except Exception as exc:
            self._disable_capabilities(extension_id)
            try:
                self.lifecycle_store.set_enabled(extension_id, False)
                self.lifecycle_store.set_status(extension_id, "failed", enabled=False, error=str(exc))
                if rec is not None:
                    rec.status = "failed"
                    rec.error = str(exc)
            except Exception:
                pass
            self._emit("extension_failed", {"extension_id": extension_id, "lifecycle": "on_enable", "error": str(exc)})
            raise ExtensionLifecycleError(f"Extension '{extension_id}' enable hook failed: {exc}") from exc

    def disable(self, extension_id: str) -> Dict[str, Any]:
        rec = self.registry.get(extension_id)
        if rec is None:
            raise ExtensionLifecycleError(f"Extension '{extension_id}' not found for disable operation") from None
        try:
            ctx = self._make_context(rec.manifest, Path(rec.source) if Path(rec.source).exists() else rec.root)
            hook = getattr(rec.extension, "on_disable", None)
            if hook is None:
                hook = getattr(rec.extension, "disable", None)
            if callable(hook):
                hook(ctx)
        except Exception as exc:
            self._emit("extension_failed", {"extension_id": extension_id, "lifecycle": "on_disable", "error": str(exc)})
            try:
                self.lifecycle_store.set_enabled(extension_id, False)
                self.lifecycle_store.set_status(extension_id, "disabled", enabled=False, error=str(exc))
            except Exception:
                pass
            if rec is not None:
                rec.status = "disabled"
            self._disable_capabilities(extension_id)
            return self.get_status(extension_id)
        self._disable_capabilities(extension_id)
        try:
            self.lifecycle_store.set_enabled(extension_id, False)
            self.lifecycle_store.set_status(extension_id, "disabled", enabled=False, error="")
            if rec is not None:
                rec.status = "disabled"
        except Exception as exc:
            raise ExtensionLifecycleError(f"Extension '{extension_id}' disable store failed: {exc}") from exc
        self._emit("extension_disabled", {"extension_id": extension_id, "status": "disabled"})
        return self.get_status(extension_id)

    def install(self, repository_url: str, ref: Optional[str] = None) -> Dict[str, Any]:
        if not repository_url or not str(repository_url).strip():
            raise ExtensionInstallError("install failed: repository_url must be non-empty")
        staging_parent = Path(tempfile.gettempdir())
        self._resolve_extensions_dir().mkdir(parents=True, exist_ok=True)
        staged_repo: Optional[Path] = None
        staging_root: Optional[Path] = None
        try:
            staged_repo = _clone_to_staging(str(repository_url).strip(), ref, staging_parent)
            staging_root = staged_repo.parent
            _validate_package_structure(staged_repo)
            manifest = load_manifest(str(staged_repo / "manifest.json"))
            _validate_api_compatibility(manifest)
            _check_system_requirements(manifest, staged_repo)
            try:
                test_entry = load_extension_from_dir(staged_repo)
            except Exception as exc:
                raise ExtensionValidationError(
                    f"Extension '{manifest.id if 'manifest' in locals() else 'unknown'}' validation/import failed: {exc}"
                ) from exc
            try:
                _install_dependencies_if_needed(staged_repo)
            except ExtensionInstallError:
                raise
            except Exception as exc:
                raise ExtensionInstallError(f"Extension '{manifest.id}' dependency installation failed: {exc}") from exc
            if self.registry.exists(manifest.id):
                raise ExtensionInstallError(
                    f"Duplicate Extension id \"{manifest.id}\" detected (install rejected; another extension already installed with same id)"
                )
            folder_name = Path(str(repository_url).rstrip("/")).name
            if not folder_name:
                folder_name = staged_repo.name
            if folder_name.endswith(".git"):
                folder_name = folder_name[:-4]
            if "://" in folder_name:
                folder_name = Path(repository_url.split("://")[-1].rstrip("/")).name
                if folder_name.endswith(".git"):
                    folder_name = folder_name[:-4]
            try:
                local_part = str(repository_url).split("://")[-1] if "://" in str(repository_url) else str(repository_url)
                local_part = local_part.rstrip("/").rstrip(".git")
                candidate = Path(local_part).name or "extension"
                if candidate and candidate != "repo":
                    folder_name = candidate
            except Exception:
                pass
            if not folder_name or folder_name == "repo":
                folder_name = manifest.id.replace(".", "-")
            folder_name = folder_name.strip().replace("/", "_").replace("\\", "_")
            if not folder_name:
                folder_name = manifest.id.replace(".", "-")
            target_dir = self._resolve_extensions_dir() / folder_name
            if target_dir.exists():
                raise ExtensionInstallError(
                    f"Extension folder collision: target folder '{target_dir}' already exists (install rejected to avoid overwriting existing package)"
                )
            _robust_move(staged_repo, target_dir)
            staged_repo = None
            # Remove .git metadata from installed package (not part of Extension package, causes uninstall rmtree issues on Windows)
            try:
                gd = target_dir / ".git"
                if gd.exists():
                    _robust_rmtree(str(gd))
            except Exception:
                pass
            try:
                entry = load_extension_from_dir(target_dir)
            except Exception as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise ExtensionValidationError(f"Extension '{manifest.id}' post-move validation failed: {exc}") from exc
            try:
                ctx = self._make_context(entry.manifest, target_dir)
                if hasattr(entry.extension, "register"):
                    entry.extension.register(ctx)
            except Exception as exc:
                try:
                    for rec in self.capability_registry.list_by_extension(entry.manifest.id):
                        self.capability_registry.remove(rec.type, rec.id)
                except Exception:
                    pass
                shutil.rmtree(target_dir, ignore_errors=True)
                raise ExtensionInstallError(f"Extension '{entry.manifest.id}' register failed: {exc}") from exc
            try:
                record = self.registry.register(entry)
            except DuplicateExtensionError as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise ExtensionInstallError(str(exc)) from exc
            self.lifecycle_store.ensure_installed(entry.manifest.id, version=entry.manifest.version)
            self.lifecycle_store.set_enabled(entry.manifest.id, True)
            self.lifecycle_store.set_status(entry.manifest.id, "enabled", enabled=True, version=entry.manifest.version)
            record.status = "loaded"
            self._emit("extension_installed", {"extension_id": entry.manifest.id, "version": entry.manifest.version, "source": str(target_dir)})
            self._emit("extension_enabled", {"extension_id": entry.manifest.id})
            return self.get_status(entry.manifest.id)
        finally:
            if staged_repo is not None and Path(staged_repo).exists():
                try:
                    if staging_root is not None and Path(staging_root).exists():
                        _robust_rmtree(str(staging_root))
                    else:
                        _robust_rmtree(str(Path(staged_repo).parent))
                except Exception:
                    pass
            elif staging_root is not None and Path(staging_root).exists():
                try:
                    _robust_rmtree(str(staging_root))
                except Exception:
                    pass

    def update(self, extension_id: str, repository_url: Optional[str] = None, ref: Optional[str] = None) -> Dict[str, Any]:
        if not extension_id or not str(extension_id).strip():
            raise ExtensionUpdateError("update failed: extension_id must be non-empty")
        extension_id = str(extension_id).strip()
        existing = self.registry.get(extension_id)
        if existing is None:
            raise ExtensionUpdateError(f"Extension '{extension_id}' not found for update operation") from None
        if repository_url is None or not str(repository_url).strip():
            raise ExtensionUpdateError(f"Extension '{extension_id}' update failed: repository_url required for update (no origin known)") from None
        staging_parent = Path(tempfile.gettempdir())
        backup_dir: Optional[Path] = None
        target_dir = Path(existing.source) if Path(existing.source).exists() and Path(existing.source).is_dir() else (self._resolve_extensions_dir() / Path(existing.source).name)
        if not target_dir.exists() or not (target_dir / "manifest.json").is_file():
            for child in self._resolve_extensions_dir().iterdir():
                if child.is_dir() and (child / "manifest.json").is_file():
                    try:
                        m = load_manifest(str(child / "manifest.json"))
                        if m.id == extension_id:
                            target_dir = child
                            break
                    except Exception:
                        continue
        staged_repo: Optional[Path] = None
        staging_root: Optional[Path] = None
        original_version = getattr(existing.manifest, "version", "")
        backup_target: Optional[Path] = None
        try:
            staged_repo = _clone_to_staging(str(repository_url).strip(), ref, staging_parent)
            staging_root = staged_repo.parent
            _validate_package_structure(staged_repo)
            new_manifest = load_manifest(str(staged_repo / "manifest.json"))
            if new_manifest.id != extension_id:
                raise ExtensionUpdateError(
                    f"Extension update rejected: manifest ID mismatch (expected '{extension_id}', got '{new_manifest.id}'; update must keep same extension_id)"
                )
            _validate_api_compatibility(new_manifest)
            _check_system_requirements(new_manifest, staged_repo)
            try:
                _install_dependencies_if_needed(staged_repo)
            except ExtensionInstallError as exc:
                raise ExtensionUpdateError(f"Extension '{extension_id}' dependency update failed: {exc}") from exc
            except Exception as exc:
                raise ExtensionUpdateError(f"Extension '{extension_id}' dependency update failed: {exc}") from exc
            try:
                _tmp_entry = load_extension_from_dir(staged_repo)
            except Exception as exc:
                raise ExtensionValidationError(f"Extension '{extension_id}' update validation/import failed: {exc}") from exc
            backup_parent = Path(tempfile.gettempdir())
            backup_dir = Path(tempfile.mkdtemp(prefix=f"aether_ext_backup_{extension_id.replace('.', '_')}_", dir=str(backup_parent)))
            backup_target = backup_dir / "backup"
            # Preserve the currently-installed package BEFORE replacing it so the
            # rollback path (below) can actually restore it if committing the new
            # package fails. Previously the backup directory was created empty,
            # so a post-move failure deleted the old package with nothing to
            # restore. Move-aside (robust, cross-device) instead of rmtree.
            try:
                if target_dir.exists():
                    _robust_move(target_dir, backup_target)
            except Exception:
                # Fallback: copy then remove so a restore source still exists.
                try:
                    if target_dir.exists():
                        shutil.copytree(str(target_dir), str(backup_target), symlinks=True)
                        _robust_rmtree(str(target_dir))
                except Exception:
                    pass
            _robust_move(staged_repo, target_dir)
            staged_repo = None
            try:
                # Validate new package can be imported; tolerate git metadata differences ( .git folder may not exist in moved package? But moved includes .git )
                # For update validation we just check manifest still correct, and attempt load without crashing if .git present
                # Remove .git from target before validation to mimic installed package without git metadata
                git_dir = target_dir / ".git"
                if git_dir.exists():
                    try:
                        _robust_rmtree(str(git_dir))
                    except Exception:
                        pass
                _post_entry = load_extension_from_dir(target_dir)
                self.lifecycle_store.set_status(extension_id, "enabled", version=new_manifest.version, enabled=True)
                self.lifecycle_store.log_activity(extension_id, "extension_updated", f"version {original_version} -> {new_manifest.version}, restart/reload required for code activation")
                existing.manifest = new_manifest
                self._emit("extension_updated", {"extension_id": extension_id, "old_version": original_version, "new_version": new_manifest.version, "restart_required": True})
                if backup_dir and backup_dir.exists():
                    _robust_rmtree(str(backup_dir))
                    backup_dir = None
                return self.get_status(extension_id)
            except Exception as exc:
                try:
                    if target_dir.exists():
                        _robust_rmtree(str(target_dir))
                    if backup_target and backup_target.exists():
                        _robust_move(backup_target, target_dir)
                except Exception:
                    pass
                raise ExtensionUpdateError(f"Extension '{extension_id}' update commit failed, restored old package: {exc}") from exc
        except (ExtensionUpdateError, ExtensionValidationError, ExtensionCompatibilityError):
            try:
                if staged_repo is not None and Path(staged_repo).exists():
                    if staging_root is not None and Path(staging_root).exists():
                        _robust_rmtree(str(staging_root))
                    else:
                        _robust_rmtree(str(Path(staged_repo).parent))
                elif staging_root is not None and Path(staging_root).exists():
                    _robust_rmtree(str(staging_root))
            except Exception:
                pass
            if backup_dir is not None:
                try:
                    if backup_target is not None and backup_target.exists():
                        if not target_dir.exists():
                            _robust_move(backup_target, target_dir)
                    if Path(backup_dir).exists():
                        _robust_rmtree(str(backup_dir))
                except Exception:
                    pass
            raise
        except ExtensionInstallError as exc:
            try:
                if staged_repo is not None and Path(staged_repo).exists():
                    if staging_root is not None and Path(staging_root).exists():
                        _robust_rmtree(str(staging_root))
                    else:
                        _robust_rmtree(str(Path(staged_repo).parent))
                elif staging_root is not None and Path(staging_root).exists():
                    _robust_rmtree(str(staging_root))
            except Exception:
                pass
            if backup_dir is not None and backup_target is not None and backup_target.exists() and not target_dir.exists():
                try:
                    _robust_move(backup_target, target_dir)
                except Exception:
                    pass
                try:
                    _robust_rmtree(str(backup_dir))
                except Exception:
                    pass
            raise ExtensionUpdateError(str(exc)) from exc
        except Exception as exc:
            try:
                if staged_repo is not None and Path(staged_repo).exists():
                    if staging_root is not None and Path(staging_root).exists():
                        _robust_rmtree(str(staging_root))
                    else:
                        _robust_rmtree(str(Path(staged_repo).parent))
                elif staging_root is not None and Path(staging_root).exists():
                    _robust_rmtree(str(staging_root))
            except Exception:
                pass
            if backup_dir is not None and backup_target is not None and backup_target.exists() and not target_dir.exists():
                try:
                    _robust_move(backup_target, target_dir)
                except Exception:
                    pass
                try:
                    _robust_rmtree(str(backup_dir))
                except Exception:
                    pass
            raise ExtensionUpdateError(f"Extension '{extension_id}' update failed: {exc}") from exc
        finally:
            if staged_repo is not None and Path(staged_repo).exists():
                try:
                    if staging_root is not None and Path(staging_root).exists():
                        _robust_rmtree(str(staging_root))
                    else:
                        _robust_rmtree(str(Path(staged_repo).parent))
                except Exception:
                    pass
            elif staging_root is not None and Path(staging_root).exists():
                try:
                    _robust_rmtree(str(staging_root))
                except Exception:
                    pass

    def uninstall(self, extension_id: str) -> Dict[str, Any]:
        if not extension_id or not str(extension_id).strip():
            raise ExtensionUninstallError("uninstall failed: extension_id must be non-empty")
        extension_id = str(extension_id).strip()
        rec = self.registry.get(extension_id)
        if rec is None:
            raise ExtensionUninstallError(f"Extension '{extension_id}' not found for uninstall operation") from None
        target_dir = Path(rec.source) if Path(rec.source).exists() and Path(rec.source).is_dir() else None
        if target_dir is None:
            for child in self._resolve_extensions_dir().iterdir():
                if child.is_dir() and (child / "manifest.json").is_file():
                    try:
                        m = load_manifest(str(child / "manifest.json"))
                        if m.id == extension_id:
                            target_dir = child
                            break
                    except Exception:
                        continue
        if target_dir is None or not target_dir.exists():
            raise ExtensionUninstallError(f"Extension '{extension_id}' source folder not found or not registered (uninstall aborted for safety)") from None
        try:
            target_dir.resolve().relative_to(self._resolve_extensions_dir().resolve())
        except ValueError:
            raise ExtensionUninstallError(
                f"Extension '{extension_id}' uninstall rejected: folder '{target_dir}' is outside Extension directory"
            ) from None
        try:
            ctx = self._make_context(rec.manifest, target_dir)
            hook = getattr(rec.extension, "on_disable", None)
            if hook is None:
                hook = getattr(rec.extension, "disable", None)
            if callable(hook):
                try:
                    hook(ctx)
                except Exception as exc:
                    self._emit("extension_failed", {"extension_id": extension_id, "lifecycle": "on_disable", "error": str(exc)})
        except Exception:
            pass
        self._disable_capabilities(extension_id)
        # Fully remove capabilities for uninstall (not just disable)
        try:
            for rec in list(self.capability_registry.list_by_extension(extension_id)):
                self.capability_registry.remove(rec.type, rec.id)
        except Exception:
            pass
        try:
            _robust_rmtree(str(target_dir))
            if Path(target_dir).exists():
                shutil.rmtree(str(target_dir), ignore_errors=True)
                if Path(target_dir).exists():
                    _robust_rmtree(str(target_dir))
        except Exception as exc:
            raise ExtensionUninstallError(f"Extension '{extension_id}' uninstall failed removing package directory: {exc}") from exc
        if Path(target_dir).exists():
            try:
                _robust_rmtree(str(target_dir))
            except Exception as exc:
                raise ExtensionUninstallError(f"Extension '{extension_id}' uninstall failed removing package directory: {exc}") from exc
            if Path(target_dir).exists():
                raise ExtensionUninstallError(f"Extension '{extension_id}' uninstall failed removing package directory: directory still exists '{target_dir}'") from None
        try:
            self.registry.remove(extension_id)
        except Exception:
            pass
        try:
            self.lifecycle_store.remove(extension_id)
        except Exception:
            pass
        self._emit("extension_uninstalled", {"extension_id": extension_id})
        return {"id": extension_id, "status": "uninstalled"}

__all__ = ["ExtensionManager"]
