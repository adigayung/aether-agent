"""Ukur milestone lengkap jalur create_task -> eksekusi (tanpa provider nyata)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "web" / "django_app"))


def timed(name, fn):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    print(f"  {name:<50} {dt:8.3f}s")
    return out


def main() -> int:
    print(f"root={ROOT}")

    # [A] TaskPreparation.prepare (synchronous, di dalam HTTP create_task)
    from agent_ai.task.preparation import TaskPreparation

    prep = TaskPreparation()
    timed("TaskPreparation().prepare(task) [cold]", lambda: prep.prepare("tulis file hello", task_id="t1"))
    timed("TaskPreparation().prepare(task) [warm]", lambda: prep.prepare("tulis file hello", task_id="t2"))

    # [B] registry build (di dalam _build_runtime)
    from agent_ai.tools.registry import build_registry

    timed("build_registry(root) [cold]", lambda: build_registry(root=ROOT))
    timed("build_registry(root) [warm]", lambda: build_registry(root=ROOT))

    # [C] extension load
    from agent_ai.extensions.agent_bridge import ensure_agent_extensions_loaded

    timed("ensure_agent_extensions_loaded [cold]", ensure_agent_extensions_loaded)

    # [D] environment detection (cold path of first task)
    from agent_ai.projects.environment import build_or_load_environment, environment_path

    p = environment_path(ROOT)
    print(f"  ENVIRONMENT.md exists: {p.exists()} -> {p}")
    timed("build_or_load_environment(root) [1st]", lambda: build_or_load_environment(ROOT))
    timed("build_or_load_environment(root) [2nd]", lambda: build_or_load_environment(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
