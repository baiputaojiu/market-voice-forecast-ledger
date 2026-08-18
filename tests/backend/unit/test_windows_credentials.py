from __future__ import annotations

import ctypes

import pytest

import market_voice_forecast_ledger.credentials.windows as windows
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.credentials.windows import (
    ERROR_NOT_FOUND,
    CRED_PERSIST_LOCAL_MACHINE,
    CRED_TYPE_GENERIC,
    YOUTUBE_API_KEY_TARGET,
    WindowsCredentialManager,
    _BOOL,
    _CredentialW,
    _CtypesCredentialFacade,
    _DWORD,
    _FileTime,
    _PCredentialW,
)


SYNTHETIC_SECRET = "synthetic-key-token-000001"


class FakeCredentialFacade:
    def __init__(self) -> None:
        self.stored_blob: bytes | None = None
        self.write_result = True
        self.read_result = True
        self.delete_result = True
        self.error_code = 0
        self.read_type = CRED_TYPE_GENERIC
        self.read_pointer_is_null = False
        self.read_blob_is_null = False
        self.raise_on: str | None = None
        self.free_error: Exception | None = None
        self.writes: list[dict[str, object]] = []
        self.reads: list[tuple[str, int]] = []
        self.deletes: list[tuple[str, int]] = []
        self.freed: list[_PCredentialW] = []
        self._allocations: list[tuple[object, ...]] = []

    def write(self, credential: _CredentialW) -> bool:
        if self.raise_on == "write":
            raise RuntimeError(
                "C:/private/native/path synthetic-key-token-000001"
            )
        blob = ctypes.string_at(
            credential.CredentialBlob,
            credential.CredentialBlobSize,
        )
        self.stored_blob = blob
        self.writes.append(
            {
                "target": credential.TargetName,
                "type": credential.Type,
                "persist": credential.Persist,
                "username": credential.UserName,
                "blob": blob,
            }
        )
        if self.write_result:
            self.stored_blob = blob
        return self.write_result

    def read(self, target: str, credential_type: int):
        self.reads.append((target, credential_type))
        if self.raise_on == "read":
            raise RuntimeError(
                "C:/private/native/path synthetic-key-token-000001"
            )
        if not self.read_result or self.stored_blob is None:
            return False, _PCredentialW()
        if self.read_pointer_is_null:
            return True, _PCredentialW()
        blob = (ctypes.c_ubyte * len(self.stored_blob)).from_buffer_copy(
            self.stored_blob
        )
        credential = _CredentialW()
        credential.Type = self.read_type
        credential.CredentialBlobSize = len(self.stored_blob)
        if not self.read_blob_is_null:
            credential.CredentialBlob = ctypes.cast(
                blob, ctypes.POINTER(ctypes.c_ubyte)
            )
        pointer = ctypes.pointer(credential)
        self._allocations.append((blob, credential, pointer))
        return True, pointer

    def delete(self, target: str, credential_type: int) -> bool:
        self.deletes.append((target, credential_type))
        if self.raise_on == "delete":
            raise RuntimeError(
                "C:/private/native/path synthetic-key-token-000001"
            )
        if self.delete_result:
            self.stored_blob = None
        return self.delete_result

    def free(self, pointer: _PCredentialW) -> None:
        self.freed.append(pointer)
        if self.free_error is not None:
            raise self.free_error

    def last_error(self) -> int:
        return self.error_code


def test_generic_current_user_credential_round_trip_uses_the_exact_target(
    capsys,
):
    facade = FakeCredentialFacade()
    store = WindowsCredentialManager(facade=facade)

    store.set_api_key(SYNTHETIC_SECRET)
    assert store.has_api_key() is True
    assert store.read_api_key() == SYNTHETIC_SECRET
    assert store.delete_api_key() is True

    assert facade.writes == [
        {
            "target": YOUTUBE_API_KEY_TARGET,
            "type": CRED_TYPE_GENERIC,
            "persist": CRED_PERSIST_LOCAL_MACHINE,
            "username": None,
            "blob": SYNTHETIC_SECRET.encode("utf-16-le"),
        }
    ]
    assert facade.reads == [
        (YOUTUBE_API_KEY_TARGET, CRED_TYPE_GENERIC),
        (YOUTUBE_API_KEY_TARGET, CRED_TYPE_GENERIC),
    ]
    assert facade.deletes == [(YOUTUBE_API_KEY_TARGET, CRED_TYPE_GENERIC)]
    assert len(facade.freed) == 2
    captured = capsys.readouterr()
    assert SYNTHETIC_SECRET not in captured.out
    assert SYNTHETIC_SECRET not in captured.err


@pytest.mark.parametrize(
    "secret",
    (
        None,
        True,
        123,
        b"synthetic-key-token-000001",
        "",
        " " * 20,
        "x" * 19,
        "x" * 201,
        "synthetic key token 000001",
        "synthetic-key-token-00001\n",
        "synthetic-key-token-非ASCII",
    ),
    ids=(
        "none",
        "bool",
        "int",
        "bytes",
        "empty",
        "blank",
        "short",
        "long",
        "space",
        "newline",
        "non-ascii",
    ),
)
def test_set_rejects_every_non_token_before_native_write(secret):
    facade = FakeCredentialFacade()
    store = WindowsCredentialManager(facade=facade)

    with pytest.raises(DomainError) as error:
        store.set_api_key(secret)

    assert error.value.code == "YOUTUBE_CREDENTIAL_INVALID"
    if str(secret):
        assert str(secret) not in str(error.value)
    assert facade.writes == []


@pytest.mark.parametrize("length", (20, 200))
def test_set_accepts_exact_ascii_token_length_boundaries(length: int):
    facade = FakeCredentialFacade()

    WindowsCredentialManager(facade=facade).set_api_key("x" * length)

    assert facade.stored_blob == ("x" * length).encode("utf-16-le")


def test_mutable_write_and_read_buffers_are_overwritten(monkeypatch):
    facade = FakeCredentialFacade()
    store = WindowsCredentialManager(facade=facade)
    wiped_buffers: list[bytearray] = []
    real_wipe = windows._wipe_buffer

    def track_wipe(buffer: bytearray) -> None:
        wiped_buffers.append(buffer)
        real_wipe(buffer)

    monkeypatch.setattr(windows, "_wipe_buffer", track_wipe)

    store.set_api_key(SYNTHETIC_SECRET)
    assert store.read_api_key() == SYNTHETIC_SECRET

    assert len(wiped_buffers) == 2
    assert all(buffer and set(buffer) == {0} for buffer in wiped_buffers)


def test_read_copies_into_mutable_storage_without_ctypes_string_at(
    monkeypatch,
):
    facade = FakeCredentialFacade()
    store = WindowsCredentialManager(facade=facade)
    store.set_api_key(SYNTHETIC_SECRET)

    def forbidden_string_at(*_args, **_kwargs):
        raise AssertionError("read made an immutable secret copy")

    monkeypatch.setattr(windows.ctypes, "string_at", forbidden_string_at)

    assert store.read_api_key() == SYNTHETIC_SECRET
    assert len(facade.freed) == 1


@pytest.mark.parametrize("operation", ("write", "read", "delete"))
def test_native_exceptions_are_replaced_by_the_exact_safe_storage_error(
    operation: str,
):
    facade = FakeCredentialFacade()
    facade.raise_on = operation
    store = WindowsCredentialManager(facade=facade)

    with pytest.raises(DomainError) as error:
        if operation == "write":
            store.set_api_key(SYNTHETIC_SECRET)
        elif operation == "read":
            store.read_api_key()
        else:
            store.delete_api_key()

    assert error.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
    assert "private" not in str(error.value).lower()
    assert SYNTHETIC_SECRET not in str(error.value)


def test_failed_native_write_is_safe_and_does_not_claim_success():
    facade = FakeCredentialFacade()
    facade.write_result = False
    facade.error_code = 5

    with pytest.raises(DomainError) as error:
        WindowsCredentialManager(facade=facade).set_api_key(SYNTHETIC_SECRET)

    assert error.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
    assert SYNTHETIC_SECRET not in str(error.value)


def test_missing_credential_is_false_for_status_and_exact_error_for_read():
    facade = FakeCredentialFacade()
    facade.read_result = False
    facade.error_code = ERROR_NOT_FOUND
    store = WindowsCredentialManager(facade=facade)

    assert store.has_api_key() is False
    with pytest.raises(DomainError) as error:
        store.read_api_key()

    assert error.value.code == "YOUTUBE_CREDENTIAL_NOT_CONFIGURED"
    assert facade.freed == []


@pytest.mark.parametrize("method", ("has", "read"))
def test_nonmissing_native_read_failure_is_safe_storage_error(method: str):
    facade = FakeCredentialFacade()
    facade.read_result = False
    facade.error_code = 5
    store = WindowsCredentialManager(facade=facade)

    with pytest.raises(DomainError) as error:
        store.has_api_key() if method == "has" else store.read_api_key()

    assert error.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
    assert "5" not in str(error.value)


@pytest.mark.parametrize(
    ("blob", "credential_type"),
    (
        (SYNTHETIC_SECRET.encode("utf-16-le"), 2),
        (SYNTHETIC_SECRET.encode("utf-16-le") + b"x", CRED_TYPE_GENERIC),
        (b"x" * 402, CRED_TYPE_GENERIC),
        (b"\x00\xd8" * 20, CRED_TYPE_GENERIC),
        (("x" * 19 + " ").encode("utf-16-le"), CRED_TYPE_GENERIC),
        (("x" * 19 + "非").encode("utf-16-le"), CRED_TYPE_GENERIC),
    ),
    ids=(
        "wrong-type",
        "odd-length",
        "oversize",
        "invalid-utf16",
        "decoded-whitespace",
        "decoded-non-ascii",
    ),
)
def test_corrupt_allocated_credential_is_invalid_and_always_freed(
    blob: bytes, credential_type: int
):
    facade = FakeCredentialFacade()
    facade.stored_blob = blob
    facade.read_type = credential_type

    with pytest.raises(DomainError) as error:
        WindowsCredentialManager(facade=facade).read_api_key()

    assert error.value.code == "YOUTUBE_CREDENTIAL_INVALID"
    assert len(facade.freed) == 1
    assert blob.hex() not in str(error.value)


def test_null_blob_is_invalid_and_successful_allocation_is_freed():
    facade = FakeCredentialFacade()
    facade.stored_blob = SYNTHETIC_SECRET.encode("utf-16-le")
    facade.read_blob_is_null = True

    with pytest.raises(DomainError) as error:
        WindowsCredentialManager(facade=facade).read_api_key()

    assert error.value.code == "YOUTUBE_CREDENTIAL_INVALID"
    assert len(facade.freed) == 1


def test_success_with_null_credential_pointer_fails_closed_without_free():
    facade = FakeCredentialFacade()
    facade.stored_blob = SYNTHETIC_SECRET.encode("utf-16-le")
    facade.read_pointer_is_null = True

    with pytest.raises(DomainError) as error:
        WindowsCredentialManager(facade=facade).read_api_key()

    assert error.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
    assert facade.freed == []


def test_free_exception_replaces_a_successful_read_with_safe_storage_error():
    facade = FakeCredentialFacade()
    facade.stored_blob = SYNTHETIC_SECRET.encode("utf-16-le")
    facade.free_error = RuntimeError("C:/private/native/free")

    with pytest.raises(DomainError) as error:
        WindowsCredentialManager(facade=facade).read_api_key()

    assert error.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
    assert len(facade.freed) == 1
    assert "private" not in str(error.value).lower()


def test_missing_delete_is_idempotent_but_other_native_failure_is_safe():
    missing = FakeCredentialFacade()
    missing.delete_result = False
    missing.error_code = ERROR_NOT_FOUND
    assert WindowsCredentialManager(facade=missing).delete_api_key() is False

    failed = FakeCredentialFacade()
    failed.delete_result = False
    failed.error_code = 5
    with pytest.raises(DomainError) as error:
        WindowsCredentialManager(facade=failed).delete_api_key()
    assert error.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"


class _FakeNativeFunction:
    def __init__(self) -> None:
        self.argtypes = None
        self.restype = None

    def __call__(self, *_args):
        return 1


class _FakeAdvapi32:
    def __init__(self) -> None:
        self.CredWriteW = _FakeNativeFunction()
        self.CredReadW = _FakeNativeFunction()
        self.CredDeleteW = _FakeNativeFunction()
        self.CredFree = _FakeNativeFunction()


def test_ctypes_facade_declares_the_exact_win32_abi_without_real_dll_load():
    library = _FakeAdvapi32()
    loader_calls: list[tuple[str, bool]] = []

    def loader(name: str, *, use_last_error: bool):
        loader_calls.append((name, use_last_error))
        return library

    _CtypesCredentialFacade(
        loader=loader,
        last_error=lambda: 0,
        running_on_windows=lambda: True,
    )

    assert loader_calls == [("Advapi32.dll", True)]
    assert ctypes.sizeof(_DWORD) == 4
    assert ctypes.sizeof(_BOOL) == 4
    assert ctypes.sizeof(_FileTime) == 8
    assert [name for name, _type in _CredentialW._fields_] == [
        "Flags",
        "Type",
        "TargetName",
        "Comment",
        "LastWritten",
        "CredentialBlobSize",
        "CredentialBlob",
        "Persist",
        "AttributeCount",
        "Attributes",
        "TargetAlias",
        "UserName",
    ]
    assert library.CredWriteW.argtypes == [_PCredentialW, _DWORD]
    assert library.CredWriteW.restype is _BOOL
    assert library.CredReadW.argtypes == [
        ctypes.c_wchar_p,
        _DWORD,
        _DWORD,
        ctypes.POINTER(_PCredentialW),
    ]
    assert library.CredReadW.restype is _BOOL
    assert library.CredDeleteW.argtypes == [
        ctypes.c_wchar_p,
        _DWORD,
        _DWORD,
    ]
    assert library.CredDeleteW.restype is _BOOL
    assert library.CredFree.argtypes == [ctypes.c_void_p]
    assert library.CredFree.restype is None


def test_non_windows_facade_fails_closed_before_attempting_dll_load():
    loader_calls: list[str] = []

    def loader(name: str, *, use_last_error: bool):
        loader_calls.append(name)
        raise AssertionError("native loader must not run")

    with pytest.raises(DomainError) as error:
        _CtypesCredentialFacade(
            loader=loader,
            last_error=lambda: 0,
            running_on_windows=lambda: False,
        )

    assert error.value.code == "YOUTUBE_CREDENTIAL_STORAGE_FAILED"
    assert loader_calls == []
