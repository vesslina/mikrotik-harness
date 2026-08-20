from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class SecretProtector(Protocol):
    name: str

    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDpapiProtector:
    """Encrypt secrets for the current Windows user through DPAPI."""

    name = "windows-dpapi-user-v1"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is available only on Windows")
        self._crypt32: Any = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        self._kernel32: Any = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = (
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _input_blob(value: bytes) -> tuple[_DataBlob, Any]:
        buffer = ctypes.create_string_buffer(value)
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return _DataBlob(len(value), pointer), buffer

    def protect(self, value: bytes) -> bytes:
        source, keepalive = self._input_blob(value)
        destination = _DataBlob()
        succeeded = self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "MikroTik Harness provider key",
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(destination),
        )
        del keepalive
        if not succeeded:
            raise OSError(ctypes.get_last_error(), "Windows DPAPI encryption failed")
        try:
            return ctypes.string_at(destination.pbData, destination.cbData)
        finally:
            self._kernel32.LocalFree(destination.pbData)

    def unprotect(self, value: bytes) -> bytes:
        source, keepalive = self._input_blob(value)
        destination = _DataBlob()
        succeeded = self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(destination),
        )
        del keepalive
        if not succeeded:
            raise OSError(ctypes.get_last_error(), "Windows DPAPI decryption failed")
        try:
            return ctypes.string_at(destination.pbData, destination.cbData)
        finally:
            self._kernel32.LocalFree(destination.pbData)


class FernetFileProtector:
    """Portable fallback using a private random key file with owner-only permissions."""

    name = "fernet-file-key-v1"

    def __init__(self, key_file: Path) -> None:
        from cryptography.fernet import Fernet

        self._fernet_type = Fernet
        self._key_file = key_file

    def _fernet(self) -> Any:
        if self._key_file.exists():
            key = self._key_file.read_bytes()
        else:
            key = self._fernet_type.generate_key()
            self._atomic_write(self._key_file, key)
        return self._fernet_type(key)

    def protect(self, value: bytes) -> bytes:
        return bytes(self._fernet().encrypt(value))

    def unprotect(self, value: bytes) -> bytes:
        return bytes(self._fernet().decrypt(value))

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            with suppress(OSError):
                path.chmod(0o600)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


@dataclass(frozen=True, slots=True)
class ProviderSecretPaths:
    file: Path
    key_file: Path


class ProviderSecretStore:
    """Small encrypted-at-rest vault keyed by provider preset name."""

    def __init__(
        self,
        paths: ProviderSecretPaths,
        protector: SecretProtector | None = None,
    ) -> None:
        self.paths = paths
        self._protector = protector or self._default_protector(paths)

    @staticmethod
    def _default_protector(paths: ProviderSecretPaths) -> SecretProtector:
        if os.name == "nt":
            try:
                protector = WindowsDpapiProtector()
                probe = protector.protect(b"mth-dpapi-probe")
                if protector.unprotect(probe) == b"mth-dpapi-probe":
                    return protector
            except OSError:
                pass
        return FernetFileProtector(paths.key_file)

    def set(self, preset_name: str, api_key: str) -> None:
        if not api_key:
            raise ValueError("API key must not be empty")
        document = self._load()
        secrets = document.setdefault("secrets", {})
        if not isinstance(secrets, dict):
            raise ValueError("Invalid provider secret mapping")
        encrypted = self._protector.protect(api_key.encode("utf-8"))
        secrets[preset_name] = {
            "protector": self._protector.name,
            "ciphertext": base64.b64encode(encrypted).decode("ascii"),
        }
        self._write(document)

    def get(self, preset_name: str) -> str | None:
        secrets = self._load().get("secrets", {})
        if not isinstance(secrets, dict):
            raise ValueError("Invalid provider secret mapping")
        entry = secrets.get(preset_name)
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid encrypted secret for preset {preset_name!r}")
        protector_name = entry.get("protector")
        if not isinstance(protector_name, str):
            raise ValueError(f"Invalid encrypted secret for preset {preset_name!r}")
        protector = self._protector_for(protector_name, preset_name)
        ciphertext = entry.get("ciphertext")
        if not isinstance(ciphertext, str):
            raise ValueError(f"Invalid encrypted secret for preset {preset_name!r}")
        encrypted = base64.b64decode(ciphertext, validate=True)
        return protector.unprotect(encrypted).decode("utf-8")

    def contains(self, preset_name: str) -> bool:
        secrets = self._load().get("secrets", {})
        return isinstance(secrets, dict) and preset_name in secrets

    def delete(self, preset_name: str) -> None:
        document = self._load()
        secrets = document.get("secrets", {})
        if not isinstance(secrets, dict) or preset_name not in secrets:
            return
        del secrets[preset_name]
        self._write(document)

    def _load(self) -> dict[str, Any]:
        if not self.paths.file.exists():
            return {"version": 1, "secrets": {}}
        loaded = json.loads(self.paths.file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Provider secret vault must contain an object")
        return loaded

    def _protector_for(self, name: str, preset_name: str) -> SecretProtector:
        """Keep vault entries readable if the preferred local protector later changes."""

        if name == self._protector.name:
            return self._protector
        if name == FernetFileProtector.name:
            return FernetFileProtector(self.paths.key_file)
        if name == WindowsDpapiProtector.name and os.name == "nt":
            return WindowsDpapiProtector()
        raise ValueError(
            f"Preset {preset_name!r} was encrypted by an unavailable protector"
        )

    def _write(self, document: dict[str, Any]) -> None:
        path = self.paths.file
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            with suppress(OSError):
                path.chmod(0o600)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
