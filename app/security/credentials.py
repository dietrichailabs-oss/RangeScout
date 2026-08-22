"""Secure, local provider credential storage.

Production credentials are stored in the current Windows user's Credential
Manager. They are deliberately excluded from ``settings.json`` and provider
objects retrieve them only when a request needs authentication.
"""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Mapping, Protocol


_TARGET_PREFIX = "RangeScout/MarketDataProvider"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_MAX_CREDENTIAL_BLOB_BYTES = 512
_ALLOWED_FIELDS = {
    "finnhub": frozenset({"api_key"}),
    "alpaca": frozenset({"key_id", "secret_key"}),
    "congress": frozenset({"api_key"}),
    "twelve_data": frozenset({"api_key"}),
    "alpha_vantage": frozenset({"api_key"}),
    "fred": frozenset({"api_key"}),
    "logo_dev": frozenset({"publishable_key"}),
}


class CredentialStorageError(RuntimeError):
    """A sanitized credential-storage failure safe for user presentation."""


@dataclass(frozen=True)
class ProviderCredentials:
    provider_id: str
    values: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        normalized_provider = self.provider_id.strip().lower()
        allowed = _ALLOWED_FIELDS.get(normalized_provider)
        if allowed is None:
            raise ValueError("Credentials are not supported for this provider.")
        normalized_values = {
            str(name).strip(): str(value).strip()
            for name, value in self.values.items()
            if str(value).strip()
        }
        if set(normalized_values) != set(allowed):
            raise ValueError("All required provider credential fields must be supplied.")
        object.__setattr__(self, "provider_id", normalized_provider)
        object.__setattr__(self, "values", normalized_values)

    def __str__(self) -> str:
        return f"ProviderCredentials(provider_id={self.provider_id!r}, values=[REDACTED])"


class CredentialStore(Protocol):
    def save(self, credentials: ProviderCredentials) -> None: ...

    def load(self, provider_id: str) -> ProviderCredentials | None: ...

    def delete(self, provider_id: str) -> bool: ...


class InMemoryCredentialStore:
    """Non-persistent test double. Production code does not select this store."""

    def __init__(self) -> None:
        self._values: dict[str, ProviderCredentials] = {}

    def save(self, credentials: ProviderCredentials) -> None:
        self._values[credentials.provider_id] = credentials

    def load(self, provider_id: str) -> ProviderCredentials | None:
        return self._values.get(_normalize_provider_id(provider_id))

    def delete(self, provider_id: str) -> bool:
        return self._values.pop(_normalize_provider_id(provider_id), None) is not None


class UnavailableCredentialStore:
    """Safe non-Windows fallback that never persists secrets."""

    def save(self, credentials: ProviderCredentials) -> None:  # noqa: ARG002
        raise CredentialStorageError("Secure credential storage is unavailable on this platform.")

    def load(self, provider_id: str) -> ProviderCredentials | None:
        _normalize_provider_id(provider_id)
        return None

    def delete(self, provider_id: str) -> bool:
        _normalize_provider_id(provider_id)
        return False


if os.name == "nt":
    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialStore:
    """Current-user Windows Credential Manager implementation."""

    def __init__(self, target_prefix: str = _TARGET_PREFIX) -> None:
        if os.name != "nt":
            raise CredentialStorageError("Secure credential storage is unavailable on this platform.")
        self._target_prefix = target_prefix.rstrip("/")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_write = self._advapi32.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL
        self._cred_read = self._advapi32.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._cred_read.restype = wintypes.BOOL
        self._cred_delete = self._advapi32.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL
        self._cred_free = self._advapi32.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    def save(self, credentials: ProviderCredentials) -> None:
        encoded = _serialize(credentials)
        if len(encoded) > _MAX_CREDENTIAL_BLOB_BYTES:
            raise CredentialStorageError("Provider credentials exceed the secure storage limit.")
        buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self._target_name(credentials.provider_id)
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = f"RangeScout:{credentials.provider_id}"
        if not self._cred_write(ctypes.byref(credential), 0):
            raise CredentialStorageError(_sanitized_windows_error("save"))

    def load(self, provider_id: str) -> ProviderCredentials | None:
        normalized = _normalize_provider_id(provider_id)
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._cred_read(
            self._target_name(normalized),
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error_code = ctypes.get_last_error()
            if error_code == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialStorageError(_sanitized_windows_error("read", error_code))
        try:
            record = pointer.contents
            blob = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
            return _deserialize(normalized, blob)
        finally:
            self._cred_free(pointer)

    def delete(self, provider_id: str) -> bool:
        normalized = _normalize_provider_id(provider_id)
        if self._cred_delete(self._target_name(normalized), _CRED_TYPE_GENERIC, 0):
            return True
        error_code = ctypes.get_last_error()
        if error_code == 1168:  # ERROR_NOT_FOUND
            return False
        raise CredentialStorageError(_sanitized_windows_error("delete", error_code))

    def _target_name(self, provider_id: str) -> str:
        return f"{self._target_prefix}/{_normalize_provider_id(provider_id)}"


def default_credential_store() -> CredentialStore:
    if os.name == "nt":
        return WindowsCredentialStore()
    return UnavailableCredentialStore()


def supported_credential_fields(provider_id: str) -> tuple[str, ...]:
    normalized = _normalize_provider_id(provider_id)
    return tuple(sorted(_ALLOWED_FIELDS.get(normalized, ())))


def _normalize_provider_id(provider_id: str) -> str:
    normalized = str(provider_id).strip().lower()
    if normalized not in _ALLOWED_FIELDS:
        raise ValueError("Credentials are not supported for this provider.")
    return normalized


def _serialize(credentials: ProviderCredentials) -> bytes:
    payload = {
        "provider_id": credentials.provider_id,
        "values": dict(credentials.values),
        "version": 1,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _deserialize(provider_id: str, blob: bytes) -> ProviderCredentials:
    try:
        payload = json.loads(blob.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("provider_id") != provider_id:
            raise ValueError
        values = payload.get("values")
        if not isinstance(values, dict):
            raise ValueError
        return ProviderCredentials(provider_id=provider_id, values=values)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CredentialStorageError("Stored provider credentials are invalid and must be replaced.") from exc


def _sanitized_windows_error(operation: str, error_code: int | None = None) -> str:
    code = ctypes.get_last_error() if error_code is None else error_code
    return f"Windows Credential Manager could not {operation} provider credentials (error {code})."
