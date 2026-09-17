"""Verifikasi Validation Subsystem.

Menguji secara deterministik:
    1. validator interface (ABC).
    2. command validator.
    3. successful command.
    4. command failure.
    5. timeout.
    6. execution error.
    7. normalized ValidationResult.
    8. runner.
    9. extensibility untuk validator tambahan.
   10. tidak ada duplicate terminal executor.

Fixture hanya di J:\\Agent_Ai\\dummy_test dan dibersihkan setelah selesai.

Jalankan:
    python scripts/check_validation.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.validation import (  # noqa: E402
    CommandValidator,
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
    ValidationRunner,
    Validator,
    ValidatorError,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "validation_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "ok.py").write_text("print('OK')\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Validation Subsystem ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    # 1) validator interface (ABC).
    assert issubclass(CommandValidator, Validator)
    try:
        Validator()  # type: ignore[abstract]
        print("[ERROR] Validator seharusnya abstract")
        return 1
    except TypeError:
        print("[1] validator interface (ABC) OK")

    # 2) command validator.
    validator = CommandValidator(root=FIXTURE)
    assert validator.name == "command"
    print(f"[2] command validator OK -> name={validator.name}")

    # 3) successful command.
    ok = validator.validate(
        ValidationRequest(target="ok", command="python ok.py")
    )
    assert isinstance(ok, ValidationResult)
    assert ok.success is True
    assert ok.outcome == ValidationOutcome.SUCCESS
    assert ok.exit_code == 0
    assert "OK" in ok.stdout
    assert ok.validator == "command"
    assert ok.duration >= 0.0
    print(f"[3] successful command OK -> outcome={ok.outcome.value} exit={ok.exit_code}")

    # 4) command failure.
    fail = validator.validate(
        ValidationRequest(target="fail", command="python -c \"import sys; sys.exit(2)\"")
    )
    assert fail.success is False
    assert fail.outcome == ValidationOutcome.COMMAND_FAILURE
    assert fail.exit_code == 2
    print(f"[4] command failure OK -> outcome={fail.outcome.value} exit={fail.exit_code}")

    # 5) timeout.
    to = validator.validate(
        ValidationRequest(
            target="slow",
            command="python -c \"import time; time.sleep(5)\"",
            timeout=0.5,
        )
    )
    assert to.success is False
    assert to.outcome == ValidationOutcome.TIMEOUT
    assert to.exit_code is None
    assert to.metadata.get("timed_out") is True
    print(f"[5] timeout OK -> outcome={to.outcome.value}")

    # 6) execution error (command tidak ditemukan -> spawn_error).
    err = validator.validate(
        ValidationRequest(target="missing", command="this_cmd_does_not_exist_xyz")
    )
    assert err.success is False
    assert err.outcome == ValidationOutcome.EXECUTION_ERROR
    print(f"[6] execution error OK -> outcome={err.outcome.value}")

    # 6b) execution error via request tanpa command -> ValidatorError.
    try:
        validator.validate(ValidationRequest(target="empty"))
        print("[ERROR] seharusnya ValidatorError untuk command kosong")
        return 1
    except ValidatorError as exc:
        print(f"[6b] request invalid ditangani OK -> {exc}")

    # 7) normalized ValidationResult.
    d = ok.to_dict()
    for key in ("success", "outcome", "exit_code", "stdout", "stderr", "duration", "validator", "metadata"):
        assert key in d, f"field '{key}' hilang dari ValidationResult"
    assert d["outcome"] == "success"
    print(f"[7] normalized ValidationResult OK -> keys={sorted(d)}")

    # 8) runner.
    runner = ValidationRunner([validator])
    assert runner.has("command")
    assert runner.list_validators() == ["command"]
    r1 = runner.run(ValidationRequest(target="ok", command="python ok.py"))
    assert r1.success is True
    r2 = runner.run(ValidationRequest(target="ok", command="python ok.py"), validator="command")
    assert r2.success is True
    all_results = runner.run_all(ValidationRequest(target="ok", command="python ok.py"))
    assert len(all_results) == 1 and all_results[0].success
    print(f"[8] runner OK -> validators={runner.list_validators()}")

    # 9) extensibility: tambah validator baru tanpa mengubah core.
    class DummyValidator(Validator):
        name = "dummy"

        def validate(self, request: ValidationRequest) -> ValidationResult:
            return ValidationResult(
                success=True,
                outcome=ValidationOutcome.SUCCESS,
                validator=self.name,
                metadata={"target": request.target},
            )

    runner.register(DummyValidator())
    assert runner.list_validators() == ["command", "dummy"]
    dummy_result = runner.run(ValidationRequest(target="x"), validator="dummy")
    assert dummy_result.success and dummy_result.validator == "dummy"
    print(f"[9] extensibility OK -> validators={runner.list_validators()}")

    # 10) tidak ada duplicate terminal executor.
    #     CommandValidator harus memakai RunCommandTool yang sudah ada.
    from agent_ai.tools.terminal import RunCommandTool

    assert isinstance(validator.tool, RunCommandTool)
    # Tidak ada modul executor terminal baru di package validation.
    validation_dir = SRC_DIR / "agent_ai" / "validation"
    files = {p.name for p in validation_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "base.py", "validators.py", "runner.py"}, files
    print("[10] tidak ada duplicate terminal executor OK (memakai RunCommandTool)")

    print()
    print("[OK] Validation Subsystem bekerja (interface, command, runner, extensible).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
