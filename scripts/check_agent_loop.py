"""Verifikasi Agent Loop (fondasi).

Menguji: AgentAction, AgentObservation, beberapa step, status DONE/FAILED,
dan ketiadaan hard limit iterasi (loop tidak berhenti karena jumlah step).

Jalankan:
    python scripts/check_agent_loop.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import (  # noqa: E402
    AgentAction,
    AgentLoop,
    AgentObservation,
    AgentStatus,
)


def main() -> int:
    print("=== Verifikasi Agent Loop ===")

    # 1) Beberapa step: action -> observation.
    loop = AgentLoop(task="Cari definisi BaseTool", max_iterations=5)
    loop.start()
    print(f"start        -> status={loop.status.value}")

    action = AgentAction(name="search_code", arguments={"query": "class BaseTool"})
    loop.record_action(action)
    print(f"record_action-> status={loop.status.value}, iter={loop.iteration}")

    obs = AgentObservation(content="1 hasil di base.py:39", action_id=action.id)
    loop.record_observation(obs)
    print(f"record_obs   -> status={loop.status.value}")

    # Step kedua.
    action2 = AgentAction(name="read_file", arguments={"path": "src/agent_ai/tools/base.py"})
    loop.record_action(action2)
    loop.record_observation(AgentObservation(content="class BaseTool(ABC): ...", action_id=action2.id))
    print(f"step kedua   -> iter={loop.iteration}, status={loop.status.value}")
    print()

    # 2) Selesai dengan DONE.
    loop.finish(result="BaseTool ditemukan.")
    print(f"finish       -> status={loop.status.value}, result={loop.state.result!r}")
    assert loop.status == AgentStatus.DONE
    assert loop.is_finished
    print()

    # 3) FAILED.
    loop2 = AgentLoop(task="task gagal", max_iterations=3)
    loop2.start()
    loop2.fail("provider tidak tersedia")
    print(f"fail         -> status={loop2.status.value}, error={loop2.state.error!r}")
    assert loop2.status == AgentStatus.FAILED
    print()

    # 4) TIDAK ada hard limit iterasi: melewati max_iterations tetap boleh.
    loop3 = AgentLoop(task="infinite", max_iterations=2)
    loop3.start()
    for i in range(5):
        loop3.record_action(AgentAction(name=f"action_{i}"))
        loop3.record_observation(AgentObservation(content=f"obs_{i}"))
    print(f"tanpa hard limit OK -> iter={loop3.iteration}, status={loop3.status.value}")
    assert loop3.iteration == 5, loop3.iteration
    assert not loop3.is_finished, "loop tidak boleh berhenti karena jumlah step"
    loop3.finish(result="selesai oleh keputusan final LLM")
    assert loop3.status == AgentStatus.DONE

    print()
    print("[OK] Agent Loop (action/observation/step, DONE/FAILED tanpa hard limit iterasi) bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
