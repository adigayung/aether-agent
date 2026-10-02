"""Tests: Global Settings AETHER (`data/settings.json`).

Membuktikan bahwa:

- `data/settings.json` adalah SATU sumber konfigurasi global: nilai yang
  dikembalikan `global_settings()` = nilai EFEKTIF dari loader yang sudah ada
  (`compression_enabled`, `write_log_response_api`, `api_retry_config`,
  `port_setting`).
- `update_global_settings()` menulis SEBAGIAN key dan MEMPERTAHANKAN key lain
  (deep-merge) sehingga setting yang belum punya kontrol UI tidak hilang.
- Validasi tipe/rentang/key tidak dikenal menolak dengan `SettingsWriteError`
  dan TIDAK mengubah file.
- `SETTINGS_PATH` diarahkan ke file SEMENTARA (tidak menyentuh settings
  produksi mesin pengembang).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_ai.config import settings as settings_mod
from agent_ai.config.settings import (
    DEFAULT_PORT,
    SettingsWriteError,
    api_retry_config,
    compression_enabled,
    global_settings,
    port_setting,
    update_global_settings,
    write_log_response_api,
)
from agent_ai.core.agent_prompt import build_agent_system_prompt


@pytest.fixture()
def settings_file(tmp_path, monkeypatch) -> Path:
    """Arahkan SETTINGS_PATH ke file sementara (isolasi total)."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", path)
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. Baca nilai AKTUAL
# --------------------------------------------------------------------------- #
def test_global_settings_reads_actual_values(settings_file):
    _write(
        settings_file,
        {
            "port": 8123,
            "compression": {"enabled": False},
            "write_log_response_api": True,
            "api_retry": {"failed_count": 12, "failed_sleep": 3},
        },
    )
    assert global_settings() == {
        "port": 8123,
        "compression": {"enabled": False},
        "write_log_response_api": True,
        "api_retry": {"failed_count": 12, "failed_sleep": 3.0},
        # Section `agent` (System Prompt Agent): `system_prompt` = nilai
        # efektif (default = prompt bawaan `agent_ai.core.agent_prompt`),
        # `default_system_prompt` = konstanta bawaan untuk tombol Restore.
        # `default_mode` = Default Execution Mode (fast/balanced/deep).
        "agent": {
            "system_prompt": build_agent_system_prompt(),
            "default_system_prompt": build_agent_system_prompt(),
            "default_mode": "balanced",
        },
    }


def test_global_settings_matches_effective_loaders(settings_file):
    """global_settings() harus identik dengan loader yang dipakai runtime."""
    _write(
        settings_file,
        {
            "port": 9100,
            "compression": {"enabled": True},
            "write_log_response_api": False,
            "api_retry": {"failed_count": 5, "failed_sleep": 1.5},
        },
    )
    actual = global_settings()
    assert actual["port"] == port_setting() == 9100
    assert actual["compression"]["enabled"] == compression_enabled() is True
    assert actual["write_log_response_api"] == write_log_response_api() is False
    retry = api_retry_config()
    assert actual["api_retry"]["failed_count"] == retry.failed_count == 5
    assert actual["api_retry"]["failed_sleep"] == retry.failed_sleep == 1.5


def test_defaults_when_file_absent(settings_file):
    assert not settings_file.exists()
    assert port_setting() == DEFAULT_PORT
    assert compression_enabled() is True  # default aman (ON)
    assert write_log_response_api() is False  # default aman (OFF)
    assert global_settings()["api_retry"] == {"failed_count": 3, "failed_sleep": 0.0}


def test_port_invalid_falls_back_to_default(settings_file):
    for bad in ("abc", None, 0, 70000, -5):
        _write(settings_file, {"port": bad})
        assert port_setting() == DEFAULT_PORT, bad


def test_missing_port_uses_default(settings_file):
    _write(settings_file, {"compression": {"enabled": False}})
    assert port_setting() == DEFAULT_PORT


# --------------------------------------------------------------------------- #
# 2. Simpan & preservasi key
# --------------------------------------------------------------------------- #
def test_update_writes_to_same_file(settings_file):
    _write(settings_file, {"compression": {"enabled": False}})
    update_global_settings({"write_log_response_api": True, "port": 9001})
    stored = _read(settings_file)
    assert stored["write_log_response_api"] is True
    assert stored["port"] == 9001
    # Efektif langsung mengikuti.
    assert write_log_response_api() is True
    assert port_setting() == 9001


def test_update_preserves_untouched_keys(settings_file):
    """Key tanpa kontrol UI TIDAK boleh hilang (deep-merge, bukan overwrite)."""
    _write(
        settings_file,
        {
            "port": 8000,
            "future_section": {"remember": "me", "n": 7},
            "compression": {"enabled": False, "level": "high"},
            "api_retry": {"failed_count": 3, "failed_sleep": 0},
        },
    )
    update_global_settings({"compression": {"enabled": True}})
    stored = _read(settings_file)
    assert stored["future_section"] == {"remember": "me", "n": 7}
    assert stored["compression"] == {"enabled": True, "level": "high"}
    assert stored["api_retry"] == {"failed_count": 3, "failed_sleep": 0}
    assert stored["port"] == 8000


def test_update_partial_section_keeps_other_field(settings_file):
    _write(settings_file, {"api_retry": {"failed_count": 12, "failed_sleep": 3}})
    update_global_settings({"api_retry": {"failed_count": 7}})
    stored = _read(settings_file)
    assert stored["api_retry"] == {"failed_count": 7, "failed_sleep": 3}
    # Gaya penulisan asli (bulat) dipertahankan: `3`, bukan `3.0`.
    assert stored["api_retry"]["failed_sleep"] == 3
    assert "3.0" not in settings_file.read_text(encoding="utf-8")


def test_update_returns_actual_values(settings_file):
    _write(settings_file, {"port": 8000})
    result = update_global_settings({"port": 8888})
    assert result == global_settings()
    assert result["port"] == 8888


# --------------------------------------------------------------------------- #
# 3. Validasi
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"port": "bukan-angka"},
        {"port": 0},
        {"port": 65536},
        {"port": -1},
        {"write_log_response_api": "yes"},
        {"compression": {"enabled": 3}},
        {"compression": "on"},
        {"api_retry": {"failed_count": -1}},
        {"api_retry": {"failed_count": "x"}},
        {"api_retry": {"failed_sleep": "abc"}},
        {"api_retry": {"failed_sleep": -2}},
        {"tidak_dikenal": True},
        {"compression": {"unknown": True}},
        {"api_retry": {"unknown": 1}},
        "bukan-object",
    ],
)
def test_update_rejects_invalid_payload(settings_file, payload):
    _write(settings_file, {"port": 8000})
    with pytest.raises(SettingsWriteError):
        update_global_settings(payload)  # type: ignore[arg-type]
    # File TIDAK boleh berubah saat payload invalid.
    assert _read(settings_file) == {"port": 8000}


def test_update_accepts_boundary_values(settings_file):
    _write(settings_file, {})
    update_global_settings(
        {"port": 1, "api_retry": {"failed_count": 0, "failed_sleep": 0}}
    )
    update_global_settings({"port": 65535})
    assert port_setting() == 65535


def test_update_atomic_leftover_tmp_absent(settings_file, tmp_path):
    """Penulisan atomik: tidak menyisakan file sementara."""
    _write(settings_file, {"port": 8000})
    update_global_settings({"port": 8001})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert _read(settings_file)["port"] == 8001
