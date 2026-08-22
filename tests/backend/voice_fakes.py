import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.voice_verification import VoiceProposal
from market_voice_forecast_ledger.voice.protocol import AdapterRequest
from market_voice_forecast_ledger.voice.runtime import RuntimeAttestation


def _write(path: Path, contents: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


def fake_runtime_attestation(
    tmp_path: Path,
) -> tuple[RuntimeAttestation, Path]:
    data_root = tmp_path / "private-data"
    runtime_root = data_root / "voice-runtime"
    model_root = data_root / "voice-models"
    work_root = data_root / "voice-work"
    work_root.mkdir(parents=True)
    python = runtime_root / "python.exe"
    yt_dlp = runtime_root / "yt-dlp.exe"
    deno = runtime_root / "deno.exe"
    ffmpeg = runtime_root / "ffmpeg.exe"
    model = model_root / "model.onnx"
    vad = model_root / "vad.onnx"
    hashes = {
        "python": _write(python, b"synthetic-python"),
        "yt_dlp": _write(yt_dlp, b"synthetic-yt-dlp"),
        "deno": _write(deno, b"synthetic-deno"),
        "ffmpeg": _write(ffmpeg, b"synthetic-ffmpeg"),
        "model": _write(model, b"synthetic-model"),
        "vad": _write(vad, b"synthetic-vad"),
    }
    return (
        RuntimeAttestation(
            python_path=python.resolve(),
            python_sha256=hashes["python"],
            python_version="3.14.6",
            yt_dlp_path=yt_dlp.resolve(),
            yt_dlp_sha256=hashes["yt_dlp"],
            yt_dlp_version="2026.08.19",
            deno_path=deno.resolve(),
            deno_sha256=hashes["deno"],
            deno_version="2.9.5",
            ffmpeg_path=ffmpeg.resolve(),
            ffmpeg_sha256=hashes["ffmpeg"],
            ffmpeg_version="9.0.1",
            model_path=model.resolve(),
            model_sha256=hashes["model"],
            model_name="model.onnx",
            model_version="model-v1",
            vad_path=vad.resolve(),
            vad_sha256=hashes["vad"],
            vad_version="vad-v1",
            provider="CPUExecutionProvider",
            adapter_contract_version="voice-adapter-v1",
            vad_contract_version="vad-v1",
            sherpa_onnx_version="1.13.4",
            sherpa_wheel_sha256=(
                "cb1834182c4047b8edb1dceeed8d5cf7d6e10295a4079e5e0fea674b4314db06"
            ),
        ),
        work_root.resolve(),
    )


@dataclass(frozen=True, slots=True)
class FakeCompleted:
    returncode: int
    stdout: bytes = b""


class FakeMediaRunner:
    def __init__(
        self,
        *,
        output: bytes | None = b"synthetic-media",
        returncode: int = 0,
        error: Exception | None = None,
        extra_name: str | None = None,
        after_call: Any = None,
    ) -> None:
        self.output = output
        self.returncode = returncode
        self.error = error
        self.extra_name = extra_name
        self.after_call = after_call
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> FakeCompleted:
        self.calls.append(tuple(argv))
        self.kwargs.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        output_path = (
            Path(argv[argv.index("-o") + 1])
            if "-o" in argv
            else Path(argv[-1])
        )
        if self.output is not None:
            output_path.write_bytes(self.output)
        if self.extra_name is not None:
            (output_path.parent / self.extra_name).write_bytes(b"unexpected")
        if self.after_call is not None:
            self.after_call(output_path)
        return FakeCompleted(self.returncode)


def valid_adapter_request(
    attestation: RuntimeAttestation,
    work_root: Path,
) -> AdapterRequest:
    audio = work_root / "job-1" / "normalized.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"synthetic-normalized-audio")
    feature = b"synthetic-reference-feature"
    return AdapterRequest.with_canonical_hash(
        adapter_contract_version=attestation.adapter_contract_version,
        audio_path=str(audio.resolve()),
        audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
        interviewer_boundary=0.2,
        model_name=attestation.model_name,
        model_path=str(attestation.model_path),
        model_sha256=attestation.model_sha256,
        model_version=attestation.model_version,
        reference_feature_b64=base64.b64encode(feature).decode("ascii"),
        reference_feature_length=len(feature),
        reference_feature_sha256=hashlib.sha256(feature).hexdigest(),
        subject_boundary=0.8,
        threshold_config_version="threshold-v1",
        vad_contract_version=attestation.vad_contract_version,
        vad_model_path=str(attestation.vad_path),
        vad_model_sha256=attestation.vad_sha256,
    )


def adapter_response_payload(request: AdapterRequest) -> bytes:
    values: dict[str, object] = {
        "adapter_contract_version": request.adapter_contract_version,
        "input_hash": request.input_hash,
        "model_name": request.model_name,
        "model_version": request.model_version,
        "proposal": VoiceProposal.LIKELY_PRESENT.value,
        "segments": [
            {
                "end_ms": 1000,
                "evidence_hash": "d" * 64,
                "ordinal": 1,
                "raw_score": 0.9,
                "start_ms": 0,
            }
        ],
        "vad_contract_version": request.vad_contract_version,
    }
    values["output_hash"] = sha256_text(canonical_json(values))
    return canonical_json(values).encode("utf-8")


class FakeAdapterRunner:
    def __init__(
        self,
        *,
        stdout: bytes | None = None,
        returncode: int = 0,
        error: Exception | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> FakeCompleted:
        self.calls.append(tuple(argv))
        self.kwargs.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        stdout = self.stdout
        if stdout is None:
            request = AdapterRequest.model_validate_json(kwargs["input"], strict=True)
            stdout = adapter_response_payload(request)
        return FakeCompleted(returncode=self.returncode, stdout=stdout)


def load_request(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    assert type(value) is dict
    return value
