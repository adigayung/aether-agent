"""Service tingkat tinggi untuk konfigurasi LLM AETHER.

Menggabungkan dua sumber:
    - `LLMConfigStore` (SQLite global): provider instance + model.
    - `EnvFile` (.env): referensi/nilai credential API key.

Menyediakan:
    - CRUD provider instance + model (relasi API Key -> Provider Instance -> Model).
    - Manajemen API key di .env yang aman (set/get/hapus/discovery).
    - Mekanisme PEMBACAAN konfigurasi untuk modul lain (`resolve_runtime_config`
      / `get_provider_config` / `get_full_config`).

Modul ini TIDAK memanggil provider dan TIDAK berisi logic Agent.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent_ai.llm_config.env_file import EnvFile, mask_secret
from agent_ai.llm_config.errors import (
    LLMConfigConflictError,
    LLMConfigNotFoundError,
    LLMConfigValidationError,
)
from agent_ai.llm_config.models import CredentialInfo, ModelConfig, ProviderInstance
from agent_ai.llm_config.providers import (
    ProviderTypeSpec,
    is_api_key_env_name,
    parse_api_key_env_name,
    provider_type_for_env_prefix,
    require_provider_type,
    suffix_label,
)
from agent_ai.llm_config.store import LLMConfigStore, default_db_path

PathLike = Union[str, "Path"]


def default_env_path() -> Path:
    """Path default file `.env` AETHER (`<repo>/.env`)."""
    from agent_ai.config.settings import PROJECT_ROOT

    return Path(PROJECT_ROOT) / ".env"


class LLMConfigService:
    """Facade CRUD konfigurasi LLM (SQLite) + credential (.env).

    Args:
        db_path: path database SQLite. Default: database global `data/aether.db`.
        env_path: path file `.env`. Default: `<repo>/.env`.
    """

    def __init__(
        self,
        db_path: Optional[PathLike] = None,
        env_path: Optional[PathLike] = None,
    ) -> None:
        self.store = LLMConfigStore(db_path if db_path is not None else default_db_path())
        self.env = EnvFile(env_path if env_path is not None else default_env_path())

    # ------------------------------------------------------------------ #
    # Validasi internal
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_provider_type(provider_type: str) -> ProviderTypeSpec:
        try:
            return require_provider_type(provider_type)
        except ValueError as exc:  # noqa: BLE001 - normalisasi ke error konfigurasi
            raise LLMConfigValidationError(str(exc)) from exc

    @staticmethod
    def _validate_api_key_env(api_key_env: str, spec: ProviderTypeSpec) -> None:
        """Validasi nama env API key terhadap provider type.

        Aturan:
            - Bila diisi, harus mengikuti pola `<PREFIX>_API_KEY[_SUFFIX]` dan
              prefix-nya harus sesuai provider type.
            - Bila provider type mewajibkan API key, `api_key_env` wajib diisi.
        """
        if api_key_env:
            parsed = parse_api_key_env_name(api_key_env)
            if parsed is None:
                raise LLMConfigValidationError(
                    f"Nama API key '{api_key_env}' tidak valid. Harus mengikuti pola "
                    f"'{spec.env_prefix}_API_KEY' atau '{spec.env_prefix}_API_KEY_<SUFFIX>'."
                )
            prefix = parsed[0]
            if prefix != spec.env_prefix:
                raise LLMConfigValidationError(
                    f"API key '{api_key_env}' bukan untuk provider type '{spec.key}'. "
                    f"Gunakan variabel dengan prefix '{spec.env_prefix}_API_KEY'."
                )
        elif spec.requires_api_key:
            raise LLMConfigValidationError(
                f"Provider type '{spec.key}' memerlukan API key. Isi 'api_key_env' "
                f"dengan nama variabel .env (mis. '{spec.env_prefix}_API_KEY')."
            )

    def _require_instance(self, instance_id: str) -> ProviderInstance:
        instance = self.store.get_provider_instance(instance_id)
        if instance is None:
            raise LLMConfigNotFoundError(
                f"Provider instance '{instance_id}' tidak ditemukan."
            )
        return instance

    def _require_model(self, model_id: str) -> ModelConfig:
        model = self.store.get_model(model_id)
        if model is None:
            raise LLMConfigNotFoundError(f"Model '{model_id}' tidak ditemukan.")
        return model

    # ------------------------------------------------------------------ #
    # CRUD: Provider Instance
    # ------------------------------------------------------------------ #
    def create_provider_instance(
        self,
        name: str,
        provider_type: str,
        api_key_env: str = "",
        api_url: str = "",
        enabled: bool = True,
    ) -> ProviderInstance:
        """Buat provider instance baru.

        Args:
            name: nama instance (unik, wajib).
            provider_type: kunci provider type (mis. "openrouter").
            api_key_env: NAMA variabel .env untuk API key. Wajib untuk provider
                cloud; boleh kosong untuk provider lokal (Ollama).
            api_url: base URL API. Kosong -> default dari provider type.
            enabled: aktif/tidak.

        Raises:
            LLMConfigValidationError: input tidak valid.
            LLMConfigConflictError: nama instance sudah dipakai.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise LLMConfigValidationError("Nama provider instance wajib diisi.")

        spec = self._require_provider_type(provider_type)
        clean_env = (api_key_env or "").strip()
        self._validate_api_key_env(clean_env, spec)

        if self.store.find_provider_instance_by_name(clean_name) is not None:
            raise LLMConfigConflictError(
                f"Provider instance dengan nama '{clean_name}' sudah ada."
            )

        clean_url = (api_url or "").strip() or spec.default_api_url
        try:
            return self.store.create_provider_instance(
                name=clean_name,
                provider_type=spec.key,
                api_url=clean_url,
                api_key_env=clean_env,
                enabled=bool(enabled),
            )
        except sqlite3.IntegrityError as exc:  # noqa: BLE001 - race/uniqueness
            raise LLMConfigConflictError(
                f"Provider instance dengan nama '{clean_name}' sudah ada."
            ) from exc

    def get_provider_instance(self, instance_id: str) -> ProviderInstance:
        """Ambil provider instance (raise bila tidak ada)."""
        return self._require_instance(instance_id)

    def list_provider_instances(self) -> List[ProviderInstance]:
        """Daftar semua provider instance."""
        return self.store.list_provider_instances()

    def update_provider_instance(
        self,
        instance_id: str,
        *,
        name: Optional[str] = None,
        provider_type: Optional[str] = None,
        api_key_env: Optional[str] = None,
        api_url: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> ProviderInstance:
        """Update provider instance.

        Hanya field yang diberikan (bukan None) yang diubah.

        Raises:
            LLMConfigNotFoundError: instance tidak ditemukan.
            LLMConfigValidationError / LLMConfigConflictError: input tidak valid.
        """
        current = self._require_instance(instance_id)

        spec = self._require_provider_type(
            provider_type if provider_type is not None else current.provider_type
        )

        fields: Dict[str, Any] = {}

        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise LLMConfigValidationError("Nama provider instance wajib diisi.")
            existing = self.store.find_provider_instance_by_name(clean_name)
            if existing is not None and existing.id != instance_id:
                raise LLMConfigConflictError(
                    f"Provider instance dengan nama '{clean_name}' sudah ada."
                )
            fields["name"] = clean_name

        # Resolusi api_key_env: gunakan nilai baru bila diberikan, else yang lama.
        resolved_env = (
            current.api_key_env if api_key_env is None else (api_key_env or "").strip()
        )
        self._validate_api_key_env(resolved_env, spec)
        if resolved_env != current.api_key_env:
            fields["api_key_env"] = resolved_env

        if provider_type is not None:
            fields["provider_type"] = spec.key

        if api_url is not None:
            fields["api_url"] = (api_url or "").strip() or spec.default_api_url
        elif provider_type is not None and not current.api_url:
            fields["api_url"] = spec.default_api_url

        if enabled is not None:
            fields["enabled"] = bool(enabled)

        if not fields:
            return current

        try:
            updated = self.store.update_provider_instance(instance_id, fields)
        except sqlite3.IntegrityError as exc:  # noqa: BLE001 - uniqueness
            raise LLMConfigConflictError(
                f"Nama provider instance '{fields.get('name')}' sudah ada."
            ) from exc
        if updated is None:  # pragma: no cover - dijaga _require_instance
            raise LLMConfigNotFoundError(
                f"Provider instance '{instance_id}' tidak ditemukan."
            )
        return updated

    def delete_provider_instance(self, instance_id: str) -> bool:
        """Hapus provider instance beserta seluruh model-nya (cascade).

        Returns:
            True bila ada instance yang dihapus.
        """
        return self.store.delete_provider_instance(instance_id)

    # ------------------------------------------------------------------ #
    # CRUD: Model
    # ------------------------------------------------------------------ #
    def add_model(
        self,
        provider_id: str,
        model_name: str,
        enabled: bool = True,
    ) -> ModelConfig:
        """Tambah model ke sebuah provider instance.

        Raises:
            LLMConfigNotFoundError: provider instance tidak ada.
            LLMConfigValidationError: nama model kosong.
            LLMConfigConflictError: model sudah terdaftar pada provider tsb.
        """
        self._require_instance(provider_id)
        clean_model = (model_name or "").strip()
        if not clean_model:
            raise LLMConfigValidationError("Nama model wajib diisi.")

        if self.store.find_model_by_name(provider_id, clean_model) is not None:
            raise LLMConfigConflictError(
                f"Model '{clean_model}' sudah terdaftar pada provider instance ini."
            )
        try:
            return self.store.create_model(
                provider_id=provider_id, model_name=clean_model, enabled=bool(enabled)
            )
        except sqlite3.IntegrityError as exc:  # noqa: BLE001 - race/uniqueness
            raise LLMConfigConflictError(
                f"Model '{clean_model}' sudah terdaftar pada provider instance ini."
            ) from exc

    def get_model(self, model_id: str) -> ModelConfig:
        """Ambil model (raise bila tidak ada)."""
        return self._require_model(model_id)

    def list_models(self, provider_id: Optional[str] = None) -> List[ModelConfig]:
        """Daftar model (semua, atau milik satu provider)."""
        if provider_id is not None:
            self._require_instance(provider_id)
        return self.store.list_models(provider_id)

    def update_model(
        self,
        model_id: str,
        *,
        model_name: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> ModelConfig:
        """Update model (nama/enabled)."""
        current = self._require_model(model_id)
        fields: Dict[str, Any] = {}

        if model_name is not None:
            clean_model = model_name.strip()
            if not clean_model:
                raise LLMConfigValidationError("Nama model wajib diisi.")
            existing = self.store.find_model_by_name(current.provider_id, clean_model)
            if existing is not None and existing.id != model_id:
                raise LLMConfigConflictError(
                    f"Model '{clean_model}' sudah terdaftar pada provider instance ini."
                )
            fields["model_name"] = clean_model

        if enabled is not None:
            fields["enabled"] = bool(enabled)

        if not fields:
            return current

        try:
            updated = self.store.update_model(model_id, fields)
        except sqlite3.IntegrityError as exc:  # noqa: BLE001 - uniqueness
            raise LLMConfigConflictError(
                f"Model '{fields.get('model_name')}' sudah terdaftar."
            ) from exc
        if updated is None:  # pragma: no cover - dijaga _require_model
            raise LLMConfigNotFoundError(f"Model '{model_id}' tidak ditemukan.")
        return updated

    def delete_model(self, model_id: str) -> bool:
        """Hapus satu model."""
        return self.store.delete_model(model_id)

    # ------------------------------------------------------------------ #
    # Credential (.env API key)
    # ------------------------------------------------------------------ #
    def _resolve_key_value(self, env_name: str) -> Optional[str]:
        """Nilai API key: file .env diprioritaskan, fallback ke environment proses."""
        value = self.env.get(env_name)
        if value is None:
            value = os.environ.get(env_name)
        return value

    def get_api_key(self, env_name: str) -> Optional[str]:
        """Ambil NILAI API key dari .env (None bila tidak ada).

        Hanya untuk konsumsi internal (runtime). JANGAN ditampilkan ke UI/API.
        """
        name = (env_name or "").strip()
        if not is_api_key_env_name(name):
            raise LLMConfigValidationError(
                f"'{env_name}' bukan nama variabel API key yang valid."
            )
        return self._resolve_key_value(name)

    def has_api_key(self, env_name: str) -> bool:
        """True bila nilai API key tersedia (di .env atau environment)."""
        if not env_name:
            return False
        return bool(self._resolve_key_value(env_name))

    def list_credentials(self) -> List[CredentialInfo]:
        """Daftar credential API key yang tersedia/dirujuk.

        Sumber (union):
            - nama variabel di .env yang cocok pola provider + `_API_KEY`.
            - nama variabel yang dirujuk oleh provider instance terdaftar
              (walau belum ada di .env -> `is_set=False`).

        Nilai secret TIDAK pernah dikembalikan; hanya `masked`.
        """
        file_entries = self.env.entries()
        file_names = {name for name in file_entries if is_api_key_env_name(name)}

        referenced: Dict[str, List[str]] = {}
        for instance in self.store.list_provider_instances():
            if instance.api_key_env:
                referenced.setdefault(instance.api_key_env, []).append(instance.name)

        all_names = sorted(file_names | set(referenced))

        infos: List[CredentialInfo] = []
        for name in all_names:
            parsed = parse_api_key_env_name(name)
            prefix, suffix = parsed if parsed is not None else ("", "")
            spec = provider_type_for_env_prefix(prefix)
            value = self._resolve_key_value(name)
            infos.append(
                CredentialInfo(
                    name=name,
                    provider_type=spec.key if spec is not None else None,
                    provider_label=spec.label if spec is not None else (prefix or "(tidak dikenal)"),
                    env_prefix=prefix,
                    suffix=suffix,
                    suffix_label=suffix_label(suffix),
                    is_set=bool(value),
                    masked=mask_secret(value),
                    used_by=sorted(referenced.get(name, [])),
                )
            )
        return infos

    def set_api_key(self, env_name: str, value: str) -> CredentialInfo:
        """Set/ubah API key di .env secara aman.

        Menulis HANYA baris variabel terkait, membuat file bila belum ada, dan
        memperbarui environment proses. Mengembalikan ringkasan (masked).
        """
        name = (env_name or "").strip()
        if not is_api_key_env_name(name):
            raise LLMConfigValidationError(
                f"Nama API key '{env_name}' tidak valid. Gunakan pola "
                f"'<PROVIDER>_API_KEY' atau '<PROVIDER>_API_KEY_<SUFFIX>'."
            )
        secret = (value or "").strip()
        if not secret:
            raise LLMConfigValidationError("Nilai API key tidak boleh kosong.")

        self.env.set(name, secret)

        parsed = parse_api_key_env_name(name)
        prefix, suffix = parsed if parsed is not None else ("", "")
        spec = provider_type_for_env_prefix(prefix)
        referenced = [
            inst.name
            for inst in self.store.list_provider_instances()
            if inst.api_key_env == name
        ]
        return CredentialInfo(
            name=name,
            provider_type=spec.key if spec is not None else None,
            provider_label=spec.label if spec is not None else (prefix or "(tidak dikenal)"),
            env_prefix=prefix,
            suffix=suffix,
            suffix_label=suffix_label(suffix),
            is_set=True,
            masked=mask_secret(secret),
            used_by=sorted(referenced),
        )

    def delete_api_key(self, env_name: str, *, force: bool = False) -> bool:
        """Hapus API key dari .env (hanya baris variabel terkait).

        Args:
            env_name: nama variabel env.
            force: bila False dan masih ada provider instance yang memakai
                credential ini, penghapusan ditolak agar tidak memutus relasi.

        Raises:
            LLMConfigValidationError: nama tidak valid, atau masih dipakai
                (tanpa `force`).
        """
        name = (env_name or "").strip()
        if not is_api_key_env_name(name):
            raise LLMConfigValidationError(
                f"Nama API key '{env_name}' tidak valid."
            )

        used_by = [
            inst.name
            for inst in self.store.list_provider_instances()
            if inst.api_key_env == name
        ]
        if used_by and not force:
            raise LLMConfigValidationError(
                f"API key '{name}' masih dipakai oleh provider instance: "
                f"{', '.join(used_by)}. Gunakan force=True bila tetap ingin menghapus."
            )
        return self.env.delete(name)

    # ------------------------------------------------------------------ #
    # Pembacaan konfigurasi untuk modul lain
    # ------------------------------------------------------------------ #
    def get_provider_config(
        self, instance_id: str, *, include_api_key: bool = False
    ) -> Dict[str, Any]:
        """Konfigurasi satu provider instance + model + status API key.

        Args:
            include_api_key: bila True, sertakan NILAI api_key (untuk runtime).
                Default False (aman untuk ditampilkan).

        Returns:
            dict: id, name, provider_type, provider_label, api_url, api_key_env,
            api_key_present, models (list), enabled. Bila `include_api_key`,
            tambahan "api_key".
        """
        instance = self._require_instance(instance_id)
        spec = provider_type_for_env_prefix(
            parse_api_key_env_name(instance.api_key_env)[0]
            if parse_api_key_env_name(instance.api_key_env)
            else ""
        )
        data = instance.to_dict()
        data["provider_label"] = spec.label if spec is not None else instance.provider_type
        data["models"] = [m.to_dict() for m in self.store.list_models(instance.id)]
        data["api_key_present"] = self.has_api_key(instance.api_key_env)
        if include_api_key:
            data["api_key"] = (
                self.get_api_key(instance.api_key_env) if instance.api_key_env else None
            )
        return data

    def resolve_runtime_config(
        self,
        instance_id: str,
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
        *,
        include_api_key: bool = True,
    ) -> Dict[str, Any]:
        """Konfigurasi siap-pakai untuk runtime (provider + model + api key).

        Mekanisme pembacaan tunggal untuk modul lain (mis. provider factory).
        Pemilihan model: `model_id` -> `model_name` -> model pertama yang enabled.

        Args:
            instance_id: id provider instance.
            model_id: id model spesifik (opsional).
            model_name: nama model spesifik (opsional).
            include_api_key: sertakan nilai api_key (default True untuk runtime).

        Returns:
            dict: instance_id, instance_name, provider_type, api_url,
            api_key_env, api_key (bila diminta), model (nama model atau ""),
            enabled.
        """
        instance = self._require_instance(instance_id)

        selected_model = ""
        if model_id is not None:
            model = self._require_model(model_id)
            if model.provider_id != instance.id:
                raise LLMConfigValidationError(
                    f"Model '{model_id}' bukan milik provider instance '{instance_id}'."
                )
            selected_model = model.model_name
        elif model_name is not None:
            selected_model = model_name
        else:
            for model in self.store.list_models(instance.id):
                if model.enabled:
                    selected_model = model.model_name
                    break

        return {
            "instance_id": instance.id,
            "instance_name": instance.name,
            "provider_type": instance.provider_type,
            "api_url": instance.api_url,
            "api_key_env": instance.api_key_env,
            "api_key": (
                self.get_api_key(instance.api_key_env)
                if (include_api_key and instance.api_key_env)
                else None
            ),
            "model": selected_model,
            "enabled": instance.enabled,
        }

    def get_full_config(self) -> List[Dict[str, Any]]:
        """Seluruh konfigurasi: provider instance + nested model (masked).

        Bentuk siap konsumsi modul lain / UI (tanpa nilai secret).
        """
        return [self.get_provider_config(inst.id) for inst in self.store.list_provider_instances()]
