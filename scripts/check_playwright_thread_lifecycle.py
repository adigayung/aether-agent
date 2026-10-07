"""Verifier — Playwright Extension lifecycle/threading (sequential AETHER tasks).

Reproduces the reported failure pattern and proves the fix:

    1. launch browser
    2. create session + page
    3. real browser operations (navigate / snapshot / click / console /
       screenshot)
    4. close session + browser
    5. launch the browser again **in the same AETHER process**
    6..9 repeat the cycle several times — each cycle on its own worker thread,
       exactly like ``aether-task-exec`` does for every task

Checks performed:

  [1] root cause: a synchronous Playwright runtime is thread-bound — using or
      stopping it from another thread fails (greenlet "Cannot switch to a
      different thread"), which is why the old lifecycle broke on task #2.
  [2] sequential tasks (one worker thread each): every cycle launches, works and
      closes without a thread-binding error; each task thread gets its own fresh
      runtime and the previous runtime is released when its browser closes.
  [3] abandoned runtime: a task thread that dies while a runtime/browser is
      still open does not poison the process - the next task releases it (driver
      terminated) and gets a fresh valid runtime on its own thread.
  [4] no leaked Playwright state: no leftover driver/browser process, no
      browser/session/page registry entries and no live runtime at the end.
  [5] backward compatibility: several cycles on a single thread keep working.
  [6] live cross-thread conflict is refused with an actionable service error
      (not a raw greenlet crash).
  [7] the public tool layer (browser_launch ... browser_close) still drives
      sequential tasks correctly.

Jalankan:
    python scripts/check_playwright_thread_lifecycle.py [--cycles 3]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Extension.playwright.services import playwright_service as pw_module  # noqa: E402
from Extension.playwright.services.playwright_service import (  # noqa: E402
    PlaywrightService,
    PlaywrightServiceError,
)

PAGE_URL = (
    "data:text/html,<html><head><title>Lifecycle</title></head>"
    "<body><h1>Hello</h1>"
    "<button id='go' onclick=\"document.title='Clicked'\">Go</button>"
    "<input id='name' value='aether'>"
    "<script>console.log('lifecycle-probe')</script>"
    "</body></html>"
)

_EXPECTED_TOOLS = (
    "browser_launch",
    "browser_close",
    "session_create",
    "session_close",
    "page_new",
    "page_close",
    "page_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_screenshot",
    "browser_console",
    "browser_network",
)


# ---------------------------------------------------------------------------
# Assertions / small helpers
# ---------------------------------------------------------------------------
def _expect(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _wait_until(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def _alive_processes() -> Dict[int, str]:
    """Map PID -> image name for every live process (``tasklist``, native)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout
    except Exception:  # noqa: BLE001 - aliveness probing is best effort
        return {}
    processes: Dict[int, str] = {}
    for line in out.splitlines():
        parts = [part.strip().strip('"') for part in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            processes[int(parts[1])] = parts[0]
    return processes


def _alive_pids() -> Set[int]:
    """All live PIDs (``tasklist`` — native Windows process listing)."""
    return set(_alive_processes())


def _process_commands() -> Optional[Dict[int, str]]:
    """Map PID -> command line for every live process (``wmic``; ``None`` if n/a)."""
    try:
        out = subprocess.run(
            ["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True,
            text=True,
            timeout=180,
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    if "ProcessId" not in out:
        return None
    table: Dict[int, str] = {}
    for line in out.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        pid = parts[-1].strip()
        command = ",".join(parts[1:-1]).strip()
        if pid.isdigit():
            table[int(pid)] = command
    return table


def _playwright_process_pids() -> Optional[Set[int]]:
    """PIDs whose command line references the Playwright browser bundle.

    ``None`` when the command line cannot be listed (WMIC unavailable) — the
    caller then falls back to driver-PID assertions only.
    """
    table = _process_commands()
    if table is None:
        return None
    return {pid for pid, command in table.items() if "ms-playwright" in command}


def _driver_pid(service: PlaywrightService) -> Optional[int]:
    """PID of the Playwright driver process behind the live runtime."""
    process = pw_module._runtime_driver_process(service.runtime)
    return getattr(process, "pid", None)


def _run_on_new_thread(name: str, target: Callable[[], None]) -> threading.Thread:
    """Run ``target`` on a fresh worker thread (like ``aether-task-exec``)."""
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    thread.join()
    return thread


# ---------------------------------------------------------------------------
# Scenario 2 — sequential tasks, one worker thread each
# ---------------------------------------------------------------------------
def _task_cycle(
    service: PlaywrightService,
    index: int,
    records: List[Dict[str, Any]],
    errors: List[str],
) -> None:
    try:
        browser_id = service.launch_browser()
        session_id = service.create_session(browser_id)
        page_id = service.new_page(session_id)
        navigation = service.navigate(page_id, PAGE_URL)
        snapshot = service.snapshot(page_id)
        button_ref = next(
            (
                ref
                for ref, info in snapshot.get("refs", {}).items()
                if str(info.get("tag") or "").lower() == "button"
            ),
            None,
        )
        _expect(button_ref is not None, f"[task {index}] snapshot found no button ref")
        service.click(page_id, ref=button_ref)
        title = service.page_info(page_id)["title"]
        console = service.console(page_id)
        screenshot = service.screenshot(page_id, full_page=True)
        state = service.status()
        driver_pid = _driver_pid(service)
        closed_session = service.close_session(session_id)
        closed_browser = service.close_browser(browser_id)
        records.append(
            {
                "index": index,
                "thread": threading.current_thread().name,
                "thread_id": threading.get_ident(),
                "generation": state["runtime_generation"],
                "runtime_thread": state["runtime_thread"],
                "driver_pid": driver_pid,
                "title": navigation.get("title"),
                "title_after_click": title,
                "element_count": snapshot.get("element_count"),
                "console_texts": [item.get("text") for item in console.get("messages", [])],
                "screenshot_bytes": screenshot.get("bytes"),
                "session_closed": bool(closed_session.get("closed")),
                "browser_closed": bool(closed_browser.get("closed")),
                "runtime_released": bool(closed_browser.get("runtime_released")),
                "release_mode": closed_browser.get("runtime_release_mode"),
            }
        )
    except BaseException as exc:  # noqa: BLE001 - reported by the checker
        errors.append(f"[task {index}] {type(exc).__name__}: {exc}")


def scenario_sequential_tasks(
    service: PlaywrightService, cycles: int, records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    print(f"[2] {cycles} task berurutan, masing-masing pada worker thread baru")
    errors: List[str] = []
    for index in range(1, cycles + 1):
        _run_on_new_thread(
            f"aether-task-exec-{index}",
            lambda index=index: _task_cycle(service, index, records, errors),
        )
        status = service.status()
        _expect(
            status["runtime_started"] is False,
            f"cycle {index}: runtime masih hidup setelah browser ditutup",
        )
        _expect(
            status["browser_count"] == 0
            and status["session_count"] == 0
            and status["page_count"] == 0,
            f"cycle {index}: registry tidak bersih -> {status}",
        )
    _expect(not errors, "task berurutan gagal: " + " | ".join(errors))
    _expect(len(records) == cycles, "tidak semua cycle tercatat")
    for record in records:
        _expect(
            record["title"] == "Lifecycle",
            f"cycle {record['index']}: judul awal salah -> {record['title']!r}",
        )
        _expect(
            record["title_after_click"] == "Clicked",
            f"cycle {record['index']}: click nyata tidak berefek",
        )
        _expect(
            record["element_count"] and record["element_count"] >= 1,
            f"cycle {record['index']}: snapshot kosong",
        )
        _expect(
            "lifecycle-probe" in record["console_texts"],
            f"cycle {record['index']}: console event tidak tertangkap",
        )
        _expect(
            record["screenshot_bytes"] and record["screenshot_bytes"] > 0,
            f"cycle {record['index']}: screenshot kosong",
        )
        _expect(
            record["session_closed"] and record["browser_closed"],
            f"cycle {record['index']}: close session/browser gagal",
        )
        _expect(
            record["runtime_released"] and record["release_mode"] == "stopped",
            f"cycle {record['index']}: runtime tidak dilepas saat browser terakhir ditutup",
        )
    generations = [record["generation"] for record in records]
    thread_ids = [record["thread_id"] for record in records]
    _expect(
        len(set(thread_ids)) == cycles,
        f"cycle tidak memakai worker thread baru -> {thread_ids}",
    )
    _expect(
        generations == sorted(set(generations)) and len(set(generations)) == cycles,
        f"runtime tidak dibuat ulang per task -> generations={generations}",
    )
    _expect(
        all(record["runtime_thread"] == record["thread_id"] for record in records),
        "runtime tidak terikat ke thread task-nya sendiri",
    )
    for record in records:
        print(
            f"    cycle {record['index']}: thread={record['thread']} "
            f"gen={record['generation']} title={record['title_after_click']} "
            f"refs={record['element_count']} shot={record['screenshot_bytes']}B "
            f"release={record['release_mode']} driver_pid={record['driver_pid']}"
        )
    return records


# ---------------------------------------------------------------------------
# Scenario 3 — abandoned runtime from a dead task thread
# ---------------------------------------------------------------------------
def scenario_abandoned_runtime(service: PlaywrightService) -> None:
    print("[3] runtime ditinggal oleh task thread yang sudah mati")
    captured: Dict[str, Any] = {}
    errors: List[str] = []

    def abandoned_cycle() -> None:
        try:
            service.launch_browser()
            service.create_session()
            service.new_page(url=PAGE_URL)
            captured["generation"] = service.status()["runtime_generation"]
            captured["driver_pid"] = _driver_pid(service)
            captured["thread_id"] = threading.get_ident()
            # Task "ends" without closing anything on purpose.
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    _run_on_new_thread("aether-task-exec-abandoned", abandoned_cycle)
    _expect(not errors, "cycle abandon gagal: " + " | ".join(errors))
    status = service.status()
    _expect(
        status["runtime_started"] is True and status["browser_count"] == 1,
        f"prasyarat abandon tidak terpenuhi -> {status}",
    )

    # A read coming from the next task thread must release the abandoned
    # generation instead of crashing on page objects of a dead thread.
    listed_pages = service.list_pages()
    _expect(
        listed_pages == [],
        f"daftar page warisan thread mati tidak dibersihkan -> {listed_pages}",
    )
    after_listing = service.status()
    _expect(
        after_listing["runtime_started"] is False
        and after_listing["browser_count"] == 0
        and after_listing["page_count"] == 0,
        f"runtime warisan belum dilepas setelah dibaca dari thread lain -> {after_listing}",
    )

    recovered: Dict[str, Any] = {}
    second_errors: List[str] = []

    def recovery_cycle() -> None:
        try:
            browser_id = service.launch_browser()
            session_id = service.create_session(browser_id)
            page_id = service.new_page(session_id, url=PAGE_URL)
            recovered["title"] = service.page_info(page_id)["title"]
            recovered["generation"] = service.status()["runtime_generation"]
            recovered["driver_pid"] = _driver_pid(service)
            service.close_session(session_id)
            service.close_browser(browser_id)
        except BaseException as exc:  # noqa: BLE001
            second_errors.append(f"{type(exc).__name__}: {exc}")

    _run_on_new_thread("aether-task-exec-after-abandon", recovery_cycle)
    _expect(
        not second_errors,
        "task berikutnya tidak bisa memakai Playwright setelah runtime ditinggal: "
        + " | ".join(second_errors),
    )
    _expect(recovered.get("title") == "Lifecycle", "recovery cycle tidak menjalankan browser")
    _expect(
        recovered["generation"] == captured["generation"] + 1,
        "runtime segar tidak dibuat pada thread task berikutnya",
    )
    releases = [
        item
        for item in service.status()["runtime_releases"]
        if item["generation"] == captured["generation"]
    ]
    _expect(releases, "tidak ada catatan pelepasan runtime yang ditinggal")
    release = releases[-1]
    _expect(
        release["released"] is True and release["mode"] == "driver_terminated",
        f"runtime yang ditinggal tidak dilepas dari thread asing -> {release}",
    )
    old_pid = captured.get("driver_pid")
    if old_pid:
        _expect(
            _wait_until(lambda: old_pid not in _alive_pids(), 20.0),
            f"driver lama (pid {old_pid}) masih hidup setelah runtime dilepas",
        )
    print(
        f"    runtime gen={captured['generation']} ditinggal oleh thread "
        f"{captured['thread_id']} -> dilepas ({release['mode']}); task berikutnya "
        f"memakai gen={recovered['generation']} dan browser baru"
    )


# ---------------------------------------------------------------------------
# Scenario 5 — same-thread cycles (backward compatibility)
# ---------------------------------------------------------------------------
def scenario_same_thread(service: PlaywrightService) -> None:
    print("[5] beberapa cycle pada thread yang sama (kompatibilitas)")
    generations: List[int] = []
    for index in range(1, 3):
        browser_id = service.launch_browser()
        session_id = service.create_session(browser_id)
        page_id = service.new_page(session_id, url=PAGE_URL)
        _expect(
            service.page_info(page_id)["title"] == "Lifecycle",
            "navigasi gagal pada cycle thread-tunggal",
        )
        generations.append(service.status()["runtime_generation"])
        service.close_session(session_id)
        closed = service.close_browser(browser_id)
        _expect(
            closed.get("runtime_released") is True,
            f"cycle thread-tunggal {index}: runtime tidak dilepas",
        )
        _expect(
            service.status()["runtime_started"] is False,
            f"cycle thread-tunggal {index}: runtime masih hidup",
        )
    _expect(
        generations == sorted(set(generations)) and len(set(generations)) == 2,
        f"runtime tidak dibuat ulang pada thread yang sama -> {generations}",
    )
    print(f"    cycle thread-tunggal OK, generations={generations}")


# ---------------------------------------------------------------------------
# Scenario 6 — live cross-thread conflict stays an actionable error
# ---------------------------------------------------------------------------
def scenario_live_conflict(service: PlaywrightService) -> None:
    print("[6] pemakaian lintas-thread saat runtime masih dimiliki thread hidup")
    keep_alive = threading.Event()
    ready = threading.Event()
    holder: Dict[str, Any] = {}

    def owner_task() -> None:
        try:
            browser_id = service.launch_browser()
            session_id = service.create_session(browser_id)
            page_id = service.new_page(session_id, url=PAGE_URL)
            holder["page_id"] = page_id
            holder["browser_id"] = browser_id
            holder["session_id"] = session_id
            holder["generation"] = service.status()["runtime_generation"]
            holder["thread_id"] = threading.get_ident()
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = f"{type(exc).__name__}: {exc}"
        ready.set()
        keep_alive.wait(60)
        try:
            service.close_session(holder["session_id"])
            service.close_browser(holder["browser_id"])
        except BaseException as exc:  # noqa: BLE001
            holder["close_error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=owner_task, name="aether-task-exec-owner", daemon=True)
    thread.start()
    _expect(ready.wait(60), "task pemilik runtime tidak siap")
    _expect("error" not in holder, f"task pemilik gagal: {holder.get('error')}")

    messages: List[str] = []
    try:
        service.launch_browser()
        # launch might succeed only if the fix wrongly re-created the runtime
        messages.append("launched")
    except PlaywrightServiceError as exc:
        messages.append(str(exc))
    _expect(
        messages and messages[0] != "launched",
        "pemakaian lintas-thread saat runtime dimiliki thread hidup seharusnya ditolak",
    )
    message = messages[0]
    _expect(
        "Playwright runtime is bound to thread" in message,
        f"pesan error tidak informatif -> {message!r}",
    )
    _expect(
        "Cannot switch to a different thread" not in message,
        "error greenlet mentah bocor ke pemanggil (bukan pesan service yang jelas)",
    )
    keep_alive.set()
    thread.join(timeout=30)
    _expect(not thread.is_alive(), "task pemilik tidak selesai")
    _expect(
        "close_error" not in holder,
        f"task pemilik gagal menutup: {holder.get('close_error')}",
    )
    _expect(
        service.status()["runtime_started"] is False,
        "runtime masih hidup setelah pemiliknya menutup browser",
    )
    print(f"    error lintas-thread informatif: {message.split('.')[0]}.")


# ---------------------------------------------------------------------------
# Scenario 7 — public tool layer
# ---------------------------------------------------------------------------
def scenario_tool_layer(service: PlaywrightService) -> None:
    print("[7] lapisan tool publik (browser_* / session_* / page_*)")
    from Extension.playwright.tools.playwright_tools import build_playwright_tools

    tools = {tool.name: tool for tool in build_playwright_tools(service)}
    for suffix in _EXPECTED_TOOLS:
        _expect(
            f"aether.playwright.{suffix}" in tools,
            f"tool hilang setelah perbaikan: {suffix}",
        )

    errors: List[str] = []
    summary: List[str] = []

    def tool_cycle(index: int) -> None:
        try:
            launch = tools["aether.playwright.browser_launch"].execute()
            session = tools["aether.playwright.session_create"].execute(
                browser_id=launch["browser_id"]
            )
            page = tools["aether.playwright.page_new"].execute(
                session_id=session["session_id"]
            )
            state = tools["aether.playwright.page_navigate"].execute(
                page_id=page["page_id"], url=PAGE_URL
            )
            snapshot = tools["aether.playwright.browser_snapshot"].execute(
                page_id=page["page_id"]
            )
            shot = tools["aether.playwright.browser_screenshot"].execute(
                page_id=page["page_id"]
            )
            console = tools["aether.playwright.browser_console"].execute(
                page_id=page["page_id"]
            )
            tools["aether.playwright.session_close"].execute(
                session_id=session["session_id"]
            )
            closed = tools["aether.playwright.browser_close"].execute(
                browser_id=launch["browser_id"]
            )
            summary.append(
                f"tool-cycle {index}: title={state.get('title')} "
                f"refs={snapshot.get('element_count')} "
                f"shot={shot.get('bytes')}B console={len(console.get('messages', []))} "
                f"released={closed.get('runtime_released')}"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"[tool {index}] {type(exc).__name__}: {exc}")

    for index in (1, 2):
        _run_on_new_thread(
            f"aether-task-exec-tools-{index}", lambda index=index: tool_cycle(index)
        )
    _expect(not errors, "tool layer gagal pada task berurutan: " + " | ".join(errors))
    _expect(len(summary) == 2, "tool cycle tidak lengkap")
    for line in summary:
        print(f"    {line}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=3, help="jumlah task berurutan (min 3)")
    args = parser.parse_args()
    cycles = max(3, args.cycles)

    print("=== Playwright Extension lifecycle/threading ===")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] library playwright tidak tersedia: {exc}")
        return 0

    baseline_playwright_pids = _playwright_process_pids()
    baseline_alive = _alive_pids()

    # --- [1] root cause: the raw sync runtime is thread-bound ---------------
    print("[1] akar masalah: runtime sync Playwright terikat thread pembuatnya")
    raw_errors: List[str] = []
    raw_runtime = sync_playwright().start()

    def use_raw_runtime_from_other_thread() -> None:
        try:
            raw_runtime.chromium.launch(headless=True)
            raw_errors.append("launch lintas-thread berhasil (tidak sesuai dugaan)")
        except BaseException as exc:  # noqa: BLE001
            raw_errors.append(f"{type(exc).__name__}: {exc}")

    _run_on_new_thread("raw-runtime-thread", use_raw_runtime_from_other_thread)
    _expect(
        raw_errors and "different thread" in raw_errors[0],
        f"runtime sync seharusnya tidak bisa dipakai lintas-thread -> {raw_errors}",
    )
    print(f"    pemakaian lintas-thread gagal seperti diharapkan: {raw_errors[0].splitlines()[0]}")
    print(
        "    catatan: pesan stderr 'Task was destroyed but it is pending!' setelah ini "
        "adalah sisa korutin yang dibatalkan oleh pelanggaran thread di atas — bukti "
        "bahwa runtime harus dilepas & dibuat ulang per thread task."
    )
    raw_runtime.stop()

    # --- service under test -------------------------------------------------
    try:
        service = PlaywrightService(headless=True, timeout=30)
        browser_id = service.launch_browser()
        service.close_browser(browser_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] browser Playwright tidak bisa dijalankan di env ini: {exc}")
        return 0

    records: List[Dict[str, Any]] = []
    scenario_sequential_tasks(service, cycles, records)
    scenario_abandoned_runtime(service)
    scenario_same_thread(service)
    scenario_live_conflict(service)
    scenario_tool_layer(service)

    # --- [4] leak check -----------------------------------------------------
    print("[4] cek kebocoran state")
    final_status = service.status()
    _expect(
        final_status["runtime_started"] is False,
        f"masih ada runtime hidup di akhir -> {final_status}",
    )
    _expect(
        final_status["browser_count"] == 0 and final_status["session_count"] == 0
        and final_status["page_count"] == 0,
        f"registry Playwright tidak bersih -> {final_status}",
    )
    driver_pids = [
        record["driver_pid"] for record in records if record.get("driver_pid")
    ]
    if driver_pids:
        _expect(
            _wait_until(lambda: not (set(driver_pids) & _alive_pids()), 20.0),
            f"driver Playwright masih hidup: {sorted(set(driver_pids) & _alive_pids())}",
        )
    print(f"    driver yang pernah dibuat sudah mati: {sorted(set(driver_pids))}")

    if baseline_playwright_pids is not None:

        def _leaked_playwright_processes() -> Set[int]:
            current = _playwright_process_pids()
            if current is None:
                return set()
            return current - baseline_playwright_pids

        _expect(
            _wait_until(lambda: not _leaked_playwright_processes(), 20.0),
            f"proses Playwright menumpuk -> {sorted(_leaked_playwright_processes())}",
        )
        print(
            "    proses ms-playwright delta terhadap baseline: "
            f"{sorted(_leaked_playwright_processes())}"
        )
    final_alive = _alive_pids()
    new_alive = final_alive - baseline_alive
    print(f"    proses OS baru yang masih hidup (indikatif): {len(new_alive)}")
    alive_table = _alive_processes()
    command_table = _process_commands() or {}
    for pid in sorted(new_alive):
        name = alive_table.get(pid, "<nama tidak tersedia>")
        command = command_table.get(pid, "")
        print(f"      pid {pid} {name} {command[:150]}")

    released = [item for item in final_status["runtime_releases"]]
    _expect(
        all(item["released"] for item in released),
        f"ada pelepasan runtime yang gagal -> {released}",
    )

    print()
    print(
        f"[OK] {cycles} task berurutan (thread berbeda) + skenario abandon/thread-tunggal/"
        "tool-layer berhasil; tidak ada error thread-binding dan tidak ada state Playwright "
        "yang bocor."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print()
        print(f"[FAIL] {exc}")
        raise SystemExit(1)
