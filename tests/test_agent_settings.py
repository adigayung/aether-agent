"""Tests: System Prompt Agent yang dapat dikelola dari Settings -> Agent.

Membuktikan bahwa:

- System Prompt Agent dibaca dari `data/settings.json` -> `agent.system_prompt`
  (SATU sumber konfigurasi global yang sama; TIDAK ada file/skema/sistem prompt
  kedua).
- Nilai DEFAULT (bila belum diatur oleh user) = isi System Prompt Agent
  existing (`agent_ai.core.agent_prompt`) sehingga behavior AETHER tetap sama.
- AgentRuntime memakai System Prompt dari settings saat membuat system message.
- Context dinamis yang sudah ada (Environment/Bible/Skill/tool) TIDAK diubah:
  orchestrator tetap menyisipkannya setelah system prompt.
- Setting lain di `data/settings.json` TIDAK hilang saat settings disimpan.
- Prompt milik pemanggil lain (mis. Consultant) TIDAK terpengaruh.

Semua test deterministik, tanpa network/LLM nyata.

Jalankan:
    python -m pytest tests/test_agent_settings.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config import settings as settings_mod  # noqa: E402
from agent_ai.config.settings import (  # noqa: E402
    SettingsWriteError,
    agent_system_prompt,
    global_settings,
    update_global_settings,
)
from agent_ai.consultant.prompt import build_consultant_system_prompt  # noqa: E402
from agent_ai.core.agent_prompt import build_agent_system_prompt  # noqa: E402
from agent_ai.providers.base import GenerateResult  # noqa: E402
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402


@pytest.fixture()
def settings_file(tmp_path, monkeypatch) -> Path:
    """Arahkan SETTINGS_PATH ke file sementara (isolasi total)."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", path)
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ScriptedProvider:
    """Provider fake minimal (tanpa network)."""

    name = "scripted"

    def generate(self, *args, **kwargs) -> GenerateResult:  # pragma: no cover
        return GenerateResult(text="", model="m", provider=self.name)


# --------------------------------------------------------------------------- #
# 1. Default = System Prompt Agent existing
# --------------------------------------------------------------------------- #
def test_default_prompt_when_file_absent(settings_file):
    assert not settings_file.exists()
    assert agent_system_prompt() == build_agent_system_prompt()


def test_default_prompt_when_section_absent(settings_file):
    _write(settings_file, {"port": 8000, "compression": {"enabled": False}})
    assert agent_system_prompt() == build_agent_system_prompt()


@pytest.mark.parametrize("bad", ["", "   ", "\n\t ", None, 5, {"x": 1}])
def test_invalid_stored_value_falls_back_to_default(settings_file, bad):
    _write(settings_file, {"agent": {"system_prompt": bad}})
    assert agent_system_prompt() == build_agent_system_prompt()


def test_file_corrupt_falls_back_to_default(settings_file):
    settings_file.write_text("{ bukan json", encoding="utf-8")
    assert agent_system_prompt() == build_agent_system_prompt()


# --------------------------------------------------------------------------- #
# 2. Baca nilai yang dikonfigurasi user
# --------------------------------------------------------------------------- #
def test_reads_configured_prompt(settings_file):
    _write(settings_file, {"agent": {"system_prompt": "PROMPT KUSTOM SAYA"}})
    assert agent_system_prompt() == "PROMPT KUSTOM SAYA"


def test_configured_prompt_overrides_default(settings_file):
    _write(settings_file, {"agent": {"system_prompt": "X"}})
    assert agent_system_prompt() != build_agent_system_prompt()


# --------------------------------------------------------------------------- #
# 3. Simpan lewat jalur konfigurasi yang SAMA + preservasi key lain
# --------------------------------------------------------------------------- #
def test_update_writes_to_same_settings_file(settings_file):
    _write(
        settings_file,
        {
            "port": 8000,
            "future_section": {"remember": "me"},
            "compression": {"enabled": False, "level": "high"},
            "api_retry": {"failed_count": 9},
        },
    )
    update_global_settings({"agent": {"system_prompt": "PROMPT BARU"}})
    stored = _read(settings_file)
    assert stored["agent"] == {"system_prompt": "PROMPT BARU"}
    # Key lain (termasuk yang belum punya kontrol UI) TETAP utuh.
    assert stored["future_section"] == {"remember": "me"}
    assert stored["compression"] == {"enabled": False, "level": "high"}
    assert stored["api_retry"] == {"failed_count": 9}
    assert stored["port"] == 8000
    # Efektif langsung mengikuti.
    assert agent_system_prompt() == "PROMPT BARU"


def test_update_partial_keeps_other_sections(settings_file):
    _write(settings_file, {"port": 8123, "agent": {"system_prompt": "AWAL"}})
    update_global_settings({"port": 9001})
    stored = _read(settings_file)
    assert stored["port"] == 9001
    assert stored["agent"]["system_prompt"] == "AWAL"


# --------------------------------------------------------------------------- #
# 4. Validasi
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"agent": "bukan-object"},
        {"agent": {"system_prompt": 5}},
        {"agent": {"system_prompt": ""}},
        {"agent": {"system_prompt": "   "}},
        {"agent": {"tidak_dikenal": True}},
        {"agent": {"system_prompt": "x" * 200_001}},
    ],
)
def test_update_rejects_invalid_payload(settings_file, payload):
    _write(settings_file, {"port": 8000})
    with pytest.raises(SettingsWriteError):
        update_global_settings(payload)
    # File TIDAK berubah saat payload invalid.
    assert _read(settings_file) == {"port": 8000}


# --------------------------------------------------------------------------- #
# 5. global_settings() = nilai efektif
# --------------------------------------------------------------------------- #
def test_global_settings_exposes_effective_prompt(settings_file):
    _write(settings_file, {"agent": {"system_prompt": "EFEKTIF"}})
    actual = global_settings()
    assert actual["agent"]["system_prompt"] == agent_system_prompt() == "EFEKTIF"
    # Bawaan ikut disertakan (konstanta, bukan setting) untuk tombol Restore.
    assert actual["agent"]["default_system_prompt"] == build_agent_system_prompt()


def test_global_settings_default_prompt_when_unset(settings_file):
    actual = global_settings()
    assert actual["agent"]["system_prompt"] == build_agent_system_prompt()
    assert actual["agent"]["default_system_prompt"] == build_agent_system_prompt()


# --------------------------------------------------------------------------- #
# 6. Runtime memakai System Prompt dari settings
# --------------------------------------------------------------------------- #
def _orchestrator_for(runtime: AgentRuntime):
    return runtime._make_orchestrator(runtime.provider)  # noqa: SLF001


def test_runtime_uses_settings_prompt(settings_file):
    _write(settings_file, {"agent": {"system_prompt": "PROMPT DARI SETTINGS"}})
    runtime = AgentRuntime(provider=ScriptedProvider(), project_root=None)
    orchestrator = _orchestrator_for(runtime)
    assert orchestrator.system_prompt == "PROMPT DARI SETTINGS"


def test_runtime_falls_back_to_default_prompt(settings_file):
    runtime = AgentRuntime(provider=ScriptedProvider(), project_root=None)
    orchestrator = _orchestrator_for(runtime)
    assert orchestrator.system_prompt == build_agent_system_prompt()


def test_runtime_respects_explicit_system_prompt(settings_file):
    _write(settings_file, {"agent": {"system_prompt": "PROMPT DARI SETTINGS"}})
    runtime = AgentRuntime(
        provider=ScriptedProvider(), system_prompt="CUSTOM CALLER", project_root=None
    )
    orchestrator = _orchestrator_for(runtime)
    assert orchestrator.system_prompt == "CUSTOM CALLER"


def test_consultant_prompt_is_unaffected(settings_file):
    """Prompt Consultant TIDAK boleh ikut berubah oleh setting Agent."""
    _write(settings_file, {"agent": {"system_prompt": "PROMPT AGENT KUSTOM"}})
    prompt = build_consultant_system_prompt("investigate")
    assert "AETHER Consultant" in prompt
    assert "PROMPT AGENT KUSTOM" not in prompt
