"""Bounded, secret-safe transport to the isolated voice adapter."""

import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.voice.media import (
    _FileIdentity,
    _file_sha256,
    _normalized_wav_duration_ms,
    _private_existing_file,
    _private_root,
    _require_attested_file,
)
from market_voice_forecast_ledger.voice.protocol import (
    MAX_ADAPTER_RESPONSE_BYTES,
    AdapterRequest,
    AdapterResponse,
    decode_response,
    encode_request,
)
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAttestation,
    verify_runtime_startup,
)


ADAPTER_TIMEOUT_SECONDS = 900
_ADAPTER_MODULE = "market_voice_forecast_ledger.voice.adapter_main"
_WINDOWS_ENV = frozenset(
    {"comspec", "pathext", "systemroot", "temp", "tmp", "windir"}
)

AdapterRunner = Callable[..., object]


@dataclass(frozen=True, slots=True)
class _Completed:
    returncode: int
    stdout: bytes


class VoiceAdapterProcess:
    def __init__(
        self,
        runner: AdapterRunner | None,
        attestation: RuntimeAttestation,
        private_work_root: Path,
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runner = runner or _bounded_run
        self._attestation = attestation
        self._private_work_root = private_work_root
        self._source_environment = (
            os.environ if source_environment is None else source_environment
        )

    def score(self, request: AdapterRequest) -> AdapterResponse:
        try:
            payload, python_identity = self._validated_payload(request)
            environment = _allowlisted_environment(self._source_environment)
            completed = self._runner(
                (
                    str(self._attestation.python_path),
                    "-I",
                    "-m",
                    _ADAPTER_MODULE,
                ),
                shell=False,
                timeout=ADAPTER_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                input=payload,
                env=environment,
                max_stdout_bytes=MAX_ADAPTER_RESPONSE_BYTES,
            )
            data_root = _private_root(self._private_work_root).parent
            _require_attested_file(
                self._attestation.python_path,
                self._attestation.python_sha256,
                data_root,
                expected_identity=python_identity,
            )
            verify_runtime_startup(self._attestation, data_root)
            returncode = getattr(completed, "returncode", None)
            stdout = getattr(completed, "stdout", None)
            if (
                type(returncode) is not int
                or returncode != 0
                or type(stdout) is not bytes
                or len(stdout) > MAX_ADAPTER_RESPONSE_BYTES
            ):
                raise ValueError("adapter process failed")
        except Exception:
            raise _process_failed() from None
        try:
            return decode_response(stdout, expected_request=request)
        except Exception:
            raise DomainError(
                "VOICE_ADAPTER_RESPONSE_INVALID", "adapter response is invalid"
            ) from None

    def _validated_payload(
        self, request: AdapterRequest
    ) -> tuple[bytes, _FileIdentity]:
        if not callable(self._runner) or not isinstance(
            self._attestation, RuntimeAttestation
        ):
            raise ValueError("invalid adapter dependencies")
        if not isinstance(request, AdapterRequest):
            raise ValueError("invalid adapter request")
        attestation = self._attestation
        if (
            attestation.provider != "CPUExecutionProvider"
            or request.adapter_contract_version
            != attestation.adapter_contract_version
            or request.vad_contract_version != attestation.vad_contract_version
            or request.model_name != attestation.model_name
            or request.model_version != attestation.model_version
            or request.model_path != str(attestation.model_path)
            or request.model_sha256 != attestation.model_sha256
            or request.vad_model_path != str(attestation.vad_path)
            or request.vad_model_sha256 != attestation.vad_sha256
        ):
            raise ValueError("adapter identity mismatch")
        work_root = _private_root(self._private_work_root)
        data_root = work_root.parent
        python_identity = _require_attested_file(
            attestation.python_path, attestation.python_sha256, data_root
        )
        verify_runtime_startup(attestation, data_root)
        _require_attested_file(
            attestation.model_path, attestation.model_sha256, data_root
        )
        _require_attested_file(
            attestation.vad_path, attestation.vad_sha256, data_root
        )
        audio_path = Path(request.audio_path)
        audio = _private_existing_file(audio_path, audio_path.parent.resolve())
        try:
            audio.relative_to(work_root)
        except ValueError:
            raise ValueError("audio escaped private work root") from None
        if (
            audio.name != "normalized.wav"
            or _file_sha256(audio) != request.audio_sha256
            or _normalized_wav_duration_ms(audio) != request.audio_duration_ms
        ):
            raise ValueError("audio identity mismatch")
        return encode_request(request), python_identity


def _allowlisted_environment(source: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(source, Mapping):
        raise ValueError("invalid environment")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in source.items():
        if type(key) is not str or type(value) is not str:
            raise ValueError("invalid environment entry")
        folded = key.casefold()
        if folded in _WINDOWS_ENV:
            if folded in seen or not value or "\x00" in value:
                raise ValueError("invalid environment entry")
            result[key] = value
            seen.add(folded)
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHONUTF8"] = "1"
    return result


def _bounded_run(
    argv: tuple[str, ...],
    *,
    shell: bool,
    timeout: int,
    check: bool,
    stdin: int,
    stdout: int,
    stderr: int,
    creationflags: int,
    input: bytes,
    env: Mapping[str, str],
    max_stdout_bytes: int,
) -> _Completed:
    if (
        type(argv) is not tuple
        or shell is not False
        or check is not False
        or stdin is not subprocess.PIPE
        or stdout is not subprocess.PIPE
        or stderr is not subprocess.DEVNULL
        or creationflags != subprocess.CREATE_NO_WINDOW
        or type(timeout) is not int
        or not 1 <= timeout <= ADAPTER_TIMEOUT_SECONDS
        or type(input) is not bytes
        or type(max_stdout_bytes) is not int
        or max_stdout_bytes != MAX_ADAPTER_RESPONSE_BYTES
    ):
        raise ValueError("invalid adapter runner contract")
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        list(argv),
        shell=False,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(env),
    )
    captured: list[bytes] = []
    failures: list[BaseException] = []

    def _exchange() -> None:
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(input)
            process.stdin.close()
            captured.append(process.stdout.read(max_stdout_bytes + 1))
            process.stdout.close()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=_exchange, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        process.kill()
        process.wait()
        thread.join()
        raise subprocess.TimeoutExpired(argv, timeout)
    if failures:
        process.kill()
        process.wait()
        raise failures[0]
    response = captured[0]
    if len(response) > max_stdout_bytes:
        process.kill()
    try:
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    return _Completed(returncode=returncode, stdout=response)


def _process_failed() -> DomainError:
    return DomainError(
        "VOICE_ADAPTER_PROCESS_FAILED", "voice adapter process failed"
    )


__all__ = ["ADAPTER_TIMEOUT_SECONDS", "VoiceAdapterProcess"]
