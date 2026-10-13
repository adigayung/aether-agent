"""Pengukuran terarah startup latency New Task (diagnosa, bukan verifier).

Mengukur milestone sepanjang jalur:
    create_task -> scheduler -> task_executor.run -> build_runtime -> run()
    -> environment context (cold) -> request provider pertama

Semua waktu memakai time.perf_counter() (monotonic). Script ini memakai
komponen AETHER existing (TaskPreparation, registry, environment detector)
dan TIDAK memanggil API provider eksternal.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Milestone recorder
_MARKS: list[tuple[str, float]] = []


def mark(name: str) -> None:
    _MARKS.append((name, time.perf_counter()))


def report(title: str) -> None:
    print(f"\n=== {title} ===")
    if not _MARKS:
        return
    base = _MARKS[0][1]
    prev = base
    for name, t in _MARKS:
        print(f"  {name:<45} +{t - base:8.3f}s  (delta {t - prev:7.3f}s)")
        prev = t


def timed(name: str, fn):
    t0 = time.perf_counter()
    out = fn()
    t1 = time.perf_counter()
    print(f"  {name:<45} {t1 - t0:8.3f}s")
    return out, (t1 - t0)


def measure_import_costs() -> None:
    print("\n=== Import cost (cold import modules) ===")
    import importlib

    for mod in (
        "agent_ai.projects.environment",
        "agent_ai.tools.registry",
        "agent_ai.extensions.agent_bridge",
        "agent_ai.runtime.runtime",
        "agent_ai.providers.registry",
    ):
        t0 = time.perf_counter()
        importlib.import_module(mod)
        print(f"  import {mod:<45} {time.perf_counter() - t0:8.3f}s")


def measure_env_detection(root: Path) -> None:
    print("\n=== Environment detection (cold, subprocess probes) ===")
    from agent_ai.projects.environment import detect_environment, render_environment

    data, total = timed("detect_environment(root)", lambda: detect_environment(root))
    print(f"    tools probed: {[t['name'] for t in data.get('tools', [])]}")

    # per-probe timing
    from agent_ai.projects.environment import _TOOL_PROBES, _first_version
    import shutil as _sh

    print("  --- per tool version probe ---")
    for name, args in _TOOL_PROBES:
        path = _sh.which(name)
        if not path:
            print(f"    {name:<12} (not in PATH)")
            continue
        t0 = time.perf_counter()
        _first_version([path, *args])
        print(f"    {name:<12} {time.perf_counter() - t0:8.3f}s")

    import time as _t

    t0 = _t.perf_counter()
    render_environment(data)
    print(f"    render_environment{'':<28} {_t.perf_counter() - t0:8.3f}s")


def measure_build_registry(root: Path) -> None:
    print("\n=== build_registry + extension load (cold) ===")
    from agent_ai.tools.registry import build_registry

    build_registry(root=root)  # warm import
    _, _ = timed("build_registry(root) [warm]", lambda: build_registry(root=root))

    from agent_ai.extensions.agent_bridge import ensure_agent_extensions_loaded

    t0 = time.perf_counter()
    ensure_agent_extensions_loaded()
    print(f"  ensure_agent_extensions_loaded (1st){'':<8} {time.perf_counter() - t0:8.3f}s")
    t0 = time.perf_counter()
    ensure_agent_extensions_loaded()
    print(f"  ensure_agent_extensions_loaded (2nd){'':<8} {time.perf_counter() - t0:8.3f}s")


def main() -> int:
    root = ROOT
    print(f"project root: {root}")
    mark("start")

    measure_import_costs()
    mark("imports")

    # Environment detection cost (this is what runs on the FIRST task of a
    # fresh runtime session when ENVIRONMENT.md does not yet exist).
    measure_env_detection(root)
    mark("env_detect")

    measure_build_registry(root)
    mark("registry")

    report("Startup milestones (monotonic)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
