from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from typing import Any, Protocol

from market_voice_forecast_ledger.credentials import CredentialStore
from market_voice_forecast_ledger.domain.errors import DomainError


YOUTUBE_API_KEY_TARGET = "MarketVoiceForecastLedger/YouTubeDataApiKey"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168

_MIN_SECRET_LENGTH = 20
_MAX_SECRET_LENGTH = 200
_MIN_BLOB_SIZE = _MIN_SECRET_LENGTH * 2
_MAX_BLOB_SIZE = _MAX_SECRET_LENGTH * 2

_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_BYTE = ctypes.c_ubyte
_LPBYTE = ctypes.POINTER(_BYTE)


class _FileTime(ctypes.Structure):
    _fields_ = (("dwLowDateTime", _DWORD), ("dwHighDateTime", _DWORD))


class _CredentialAttributeW(ctypes.Structure):
    _fields_ = (
        ("Keyword", ctypes.c_wchar_p),
        ("Flags", _DWORD),
        ("ValueSize", _DWORD),
        ("Value", _LPBYTE),
    )


class _CredentialW(ctypes.Structure):
    _fields_ = (
        ("Flags", _DWORD),
        ("Type", _DWORD),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", _DWORD),
        ("CredentialBlob", _LPBYTE),
        ("Persist", _DWORD),
        ("AttributeCount", _DWORD),
        ("Attributes", ctypes.POINTER(_CredentialAttributeW)),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    )


_PCredentialW = ctypes.POINTER(_CredentialW)


class _CredentialFacade(Protocol):
    def write(self, credential: _CredentialW) -> bool: ...

    def read(
        self, target: str, credential_type: int
    ) -> tuple[bool, _PCredentialW]: ...

    def delete(self, target: str, credential_type: int) -> bool: ...

    def free(self, pointer: _PCredentialW) -> None: ...

    def last_error(self) -> int: ...


def _credential_not_configured() -> DomainError:
    return DomainError(
        "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
        "YouTube credential is not configured",
    )


def _credential_invalid() -> DomainError:
    return DomainError(
        "YOUTUBE_CREDENTIAL_INVALID",
        "YouTube credential is invalid",
    )


def _credential_storage_failed() -> DomainError:
    return DomainError(
        "YOUTUBE_CREDENTIAL_STORAGE_FAILED",
        "YouTube credential storage failed",
    )


def _running_on_windows() -> bool:
    return os.name == "nt"


def _load_advapi32(name: str, *, use_last_error: bool) -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise _credential_storage_failed()
    return loader(name, use_last_error=use_last_error)


def _get_last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        raise _credential_storage_failed()
    return int(getter())


class _CtypesCredentialFacade:
    def __init__(
        self,
        *,
        loader: Callable[..., Any] = _load_advapi32,
        last_error: Callable[[], int] = _get_last_error,
        running_on_windows: Callable[[], bool] = _running_on_windows,
    ) -> None:
        try:
            is_windows = running_on_windows()
        except Exception:
            raise _credential_storage_failed() from None
        if is_windows is not True:
            raise _credential_storage_failed()
        try:
            library = loader("Advapi32.dll", use_last_error=True)
            self._cred_write = library.CredWriteW
            self._cred_read = library.CredReadW
            self._cred_delete = library.CredDeleteW
            self._cred_free = library.CredFree
            self._cred_write.argtypes = [_PCredentialW, _DWORD]
            self._cred_write.restype = _BOOL
            self._cred_read.argtypes = [
                ctypes.c_wchar_p,
                _DWORD,
                _DWORD,
                ctypes.POINTER(_PCredentialW),
            ]
            self._cred_read.restype = _BOOL
            self._cred_delete.argtypes = [
                ctypes.c_wchar_p,
                _DWORD,
                _DWORD,
            ]
            self._cred_delete.restype = _BOOL
            self._cred_free.argtypes = [ctypes.c_void_p]
            self._cred_free.restype = None
        except Exception:
            raise _credential_storage_failed() from None
        self._last_error = last_error

    def write(self, credential: _CredentialW) -> bool:
        return bool(self._cred_write(ctypes.byref(credential), 0))

    def read(
        self, target: str, credential_type: int
    ) -> tuple[bool, _PCredentialW]:
        pointer = _PCredentialW()
        succeeded = self._cred_read(
            target,
            credential_type,
            0,
            ctypes.byref(pointer),
        )
        return bool(succeeded), pointer

    def delete(self, target: str, credential_type: int) -> bool:
        return bool(self._cred_delete(target, credential_type, 0))

    def free(self, pointer: _PCredentialW) -> None:
        self._cred_free(ctypes.cast(pointer, ctypes.c_void_p))

    def last_error(self) -> int:
        return int(self._last_error())


def _validate_secret(secret: object) -> str:
    if type(secret) is not str:
        raise _credential_invalid()
    if not _MIN_SECRET_LENGTH <= len(secret) <= _MAX_SECRET_LENGTH:
        raise _credential_invalid()
    if any(not 0x21 <= ord(character) <= 0x7E for character in secret):
        raise _credential_invalid()
    return secret


def _wipe_buffer(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


def _is_missing(facade: _CredentialFacade) -> bool:
    try:
        return facade.last_error() == ERROR_NOT_FOUND
    except Exception:
        return False


class WindowsCredentialManager(CredentialStore):
    def __init__(self, facade: _CredentialFacade | None = None) -> None:
        self._facade = facade if facade is not None else _CtypesCredentialFacade()

    def set_api_key(self, secret: str) -> None:
        validated = _validate_secret(secret)
        try:
            blob = bytearray(validated.encode("utf-16-le"))
        except Exception:
            raise _credential_invalid() from None
        try:
            native_blob = (_BYTE * len(blob)).from_buffer(blob)
            credential = _CredentialW()
            credential.Type = CRED_TYPE_GENERIC
            credential.TargetName = YOUTUBE_API_KEY_TARGET
            credential.CredentialBlobSize = len(blob)
            credential.CredentialBlob = ctypes.cast(native_blob, _LPBYTE)
            credential.Persist = CRED_PERSIST_LOCAL_MACHINE
            credential.UserName = None
            try:
                succeeded = self._facade.write(credential)
            except Exception:
                raise _credential_storage_failed() from None
            if succeeded is not True:
                raise _credential_storage_failed()
        finally:
            _wipe_buffer(blob)

    def has_api_key(self) -> bool:
        try:
            self._read_api_key()
        except DomainError as error:
            if error.code == "YOUTUBE_CREDENTIAL_NOT_CONFIGURED":
                return False
            raise
        return True

    def read_api_key(self) -> str:
        return self._read_api_key()

    def delete_api_key(self) -> bool:
        try:
            succeeded = self._facade.delete(
                YOUTUBE_API_KEY_TARGET, CRED_TYPE_GENERIC
            )
        except Exception:
            raise _credential_storage_failed() from None
        if succeeded is True:
            return True
        if _is_missing(self._facade):
            return False
        raise _credential_storage_failed()

    def _read_api_key(self) -> str:
        try:
            succeeded, pointer = self._facade.read(
                YOUTUBE_API_KEY_TARGET, CRED_TYPE_GENERIC
            )
        except Exception:
            raise _credential_storage_failed() from None
        if succeeded is not True:
            if _is_missing(self._facade):
                raise _credential_not_configured()
            raise _credential_storage_failed()
        if not bool(pointer):
            raise _credential_storage_failed()

        secret: str | None = None
        error: DomainError | None = None
        local_blob: bytearray | None = None
        try:
            credential = pointer.contents
            blob_size = int(credential.CredentialBlobSize)
            if credential.Type != CRED_TYPE_GENERIC:
                raise _credential_invalid()
            if (
                blob_size < _MIN_BLOB_SIZE
                or blob_size > _MAX_BLOB_SIZE
                or blob_size % 2 != 0
                or not bool(credential.CredentialBlob)
            ):
                raise _credential_invalid()
            local_blob = bytearray(blob_size)
            destination = (_BYTE * blob_size).from_buffer(local_blob)
            ctypes.memmove(destination, credential.CredentialBlob, blob_size)
            try:
                decoded = local_blob.decode("utf-16-le")
            except UnicodeError:
                raise _credential_invalid() from None
            secret = _validate_secret(decoded)
        except DomainError as caught:
            error = caught
        except Exception:
            error = _credential_storage_failed()
        finally:
            if local_blob is not None:
                _wipe_buffer(local_blob)
            try:
                self._facade.free(pointer)
            except Exception:
                if error is None:
                    error = _credential_storage_failed()

        if error is not None:
            raise error
        if secret is None:
            raise _credential_storage_failed()
        return secret


__all__ = ["CredentialStore", "WindowsCredentialManager", "YOUTUBE_API_KEY_TARGET"]
