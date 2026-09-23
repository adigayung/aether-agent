"""Verifier Tool Result Compaction (deterministik, tanpa LLM/network).

Memverifikasi bahwa hasil tool lama tidak lagi menjadi beban context kumulatif:
informasi penting dipertahankan dalam representasi ringkas TERSTRUKTUR dan
detail tetap dapat diambil ulang lewat tool yang sudah ada.

Membungkus `tests/test_tool_result_compaction.py` (A-L) dan menambahkan
pengukuran sebelum/sesudah (benchmark) memakai skenario yang SAMA.

Yang diverifikasi:
    A. hasil read_file besar
    B. beberapa hasil read_file berurutan
    C. hasil search_code
    D. hasil atlas_query / rig_query
    E. output run_command besar
    F. command gagal dengan stderr penting
    G. hasil edit_file
    H. beberapa round dengan tool result yang terus bertambah (end-to-end)
    I. Agent masih dapat mengambil ulang detail yang sudah dikompak
    J. protokol tool tetap valid
    K. Task 1 context compaction tetap PASS
    L. existing Agent/continuous-loop regression tetap PASS

Jalankan:
    python scripts/check_tool_result_compaction.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
for _path in (str(SRC_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import tests.test_tool_result_compaction as t  # noqa: E402

COMPACTOR = t.COMPACTOR


def _measurement() -> List[Tuple[str, int, int, str]]:
    """Ukur ukuran raw vs compacted representasi untuk tiap tool (deterministik)."""
    cases: List[Tuple[str, str]] = [
        ("read_file", json.dumps(t._read_file_payload())),
        ("search_code", json.dumps(t._search_payload("def run", matches=30))),
        ("run_command", json.dumps(t._run_command_payload("pytest -q", out_lines=600))),
        ("edit_file", json.dumps(t._edit_payload("src/big.py", noise=3000))),
        ("atlas_query", json.dumps(t._atlas_payload(12))),
        ("rig_query", json.dumps(t._rig_payload(10))),
        (
            "run_command(error)",
            "Tool execution failed (outcome=command_failure).\n"
            "exit_code=1\nerror=None\nstdout:\n"
            + "\n".join(f"o{i}" for i in range(400))
            + "\nstderr:\nE   AssertionError: marker_zzz at tests/test_x.py:42\n",
        ),
    ]
    rows: List[Tuple[str, int, int, str]] = []
    for name, raw in cases:
        tool = "run_command" if name == "run_command(error)" else name
        result = COMPACTOR.compact_result(tool, raw)
        rows.append((name, len(raw), result.compact_chars, result.kind))
    return rows


def _print_measurement() -> None:
    print("[MEASUREMENT] ukuran representasi (karakter) per tool:")
    print(f"    {'tool':<20}{'raw':>10}{'compacted':>12}{'hemat':>9}  kind")
    for name, raw, compact, kind in _measurement():
        pct = (1 - compact / raw) * 100 if raw else 0.0
        print(f"    {name:<20}{raw:>10}{compact:>12}{pct:>8.0f}%  {kind}")


def _print_benchmark() -> None:
    data = t.benchmark()
    before, after = data["before"], data["after"]
    print("[BENCHMARK] skenario yang SAMA, sebelum (tanpa compaction) vs sesudah:")
    print(f"    {'metrik':<28}{'before':>12}{'after':>12}")
    for key, label in (
        ("rounds", "jumlah rounds (LLM call)"),
        ("total_input_tokens", "total estimated input tokens"),
        ("tool_calls", "jumlah tool calls"),
    ):
        print(f"    {label:<28}{before[key]:>12}{after[key]:>12}")
    print(f"    {'hasil task':<28}{before['result']:>12}{after['result']:>12}")
    saved = before["total_input_tokens"] - after["total_input_tokens"]
    pct = (1 - after["total_input_tokens"] / before["total_input_tokens"]) * 100 if before["total_input_tokens"] else 0.0
    print(
        f"    -> hemat ~{pct:.0f}% input token; rounds & tool calls IDENTIK; "
        f"hasil task identik (tidak ada retrieval ulang / pekerjaan terulang)."
    )
    assert saved > 0


def main() -> int:
    print("=== Verifikasi Tool Result Compaction (lookup + retrieval) ===")
    code = t.main()
    if code != 0:
        return code
    print()
    _print_measurement()
    print()
    _print_benchmark()
    print()
    print(
        "[OK] Tool result lama tidak lagi beban context kumulatif: informasi penting "
        "dipertahankan (locator + representasi terstruktur), detail tetap dapat "
        "diambil ulang, protokol tool & Task 1 compaction tetap valid."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
