import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.voice_verification import VoiceProposal
from market_voice_forecast_ledger.services.voice_reference import (
    ApprovedReferenceClip,
    PreparedReferenceAudio,
    ReferenceFeatureData,
    ReferenceMediaPlan,
)
from market_voice_forecast_ledger.voice.protocol import (
    AdapterRequest,
    AdapterResponse,
)
from market_voice_forecast_ledger.voice.runtime import RuntimeAttestation


class SimulatedPresenceCrash(BaseException):
    pass


class SyntheticReferenceMedia:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.calls: list[tuple[str, int, int]] = []

    def plan(
        self,
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
    ) -> ReferenceMediaPlan:
        model_key = hashlib.sha256(
            model.model_name.encode("ascii")
        ).hexdigest()[:12]
        job_dir = (
            self.root
            / model_key
            / (
                f"{approval.subject_id}-{approval.ordinal}-"
                f"{approval.approval_hash[:12]}"
            )
        ).resolve()
        job_dir.mkdir(parents=True, exist_ok=False)
        return ReferenceMediaPlan(
            model_sha256=model.model_sha256,
            approval_hash=approval.approval_hash,
            video_id=approval.video_id,
            source_path=job_dir / "source.media",
            source_part_path=job_dir / "source.media.part",
            normalized_path=job_dir / "normalized.wav",
        )

    def prepare(
        self,
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
        plan: ReferenceMediaPlan,
    ) -> PreparedReferenceAudio:
        self.calls.append(
            (model.model_name, approval.subject_id, approval.ordinal)
        )
        source = (
            f"synthetic-source:{model.model_name}:"
            f"{approval.subject_id}:{approval.ordinal}"
        ).encode("ascii")
        partial = b"synthetic-registered-partial"
        normalized = _synthetic_pcm_wav(duration_ms=approval.end_ms)
        plan.source_path.write_bytes(source)
        plan.source_part_path.write_bytes(partial)
        plan.normalized_path.write_bytes(normalized)
        return PreparedReferenceAudio(
            local_path=plan.normalized_path,
            audio_duration_ms=approval.end_ms,
            normalized_audio_sha256=hashlib.sha256(normalized).hexdigest(),
        )


class SyntheticReferenceScorer:
    def __init__(self) -> None:
        self.enrollment_calls: list[tuple[str, int, tuple[int, ...]]] = []
        self.score_calls: list[tuple[str, int, int]] = []
        self._dry_run_calls: list[tuple[str, int]] = []

    @property
    def dry_run_calls(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._dry_run_calls)

    def derive_enrollment_feature(
        self,
        model: RuntimeAttestation,
        subject_id: int,
        clips: tuple[
            tuple[ApprovedReferenceClip, PreparedReferenceAudio], ...
        ],
    ) -> ReferenceFeatureData:
        self.enrollment_calls.append(
            (
                model.model_name,
                subject_id,
                tuple(approval.ordinal for approval, _audio in clips),
            )
        )
        is_campplus = "campplus" in model.model_name
        dimension = 192 if is_campplus else 256
        marker = 1.0 if is_campplus else 2.0
        body = struct.pack(
            f"<{dimension}f",
            marker,
            float(subject_id),
            *([0.0] * (dimension - 2)),
        )
        return ReferenceFeatureData(
            subject_id=subject_id,
            encoding_version="sherpa-speaker-embedding-v1",
            float_dtype="float32-le",
            dimension=dimension,
            embedding_blob=body,
            feature_sha256=hashlib.sha256(body).hexdigest(),
        )

    def score(
        self,
        model: RuntimeAttestation,
        subject_id: int,
        feature: ReferenceFeatureData,
        approval: ApprovedReferenceClip,
        audio: PreparedReferenceAudio,
    ) -> float:
        del feature, audio
        self.score_calls.append(
            (model.model_name, subject_id, approval.ordinal)
        )
        positive, negative = (
            (0.80, 0.30)
            if "campplus" in model.model_name
            else (0.75, 0.10)
        )
        return positive if approval.clip_kind == "held_out_positive" else negative

    def dry_run(
        self,
        model: RuntimeAttestation,
        features: tuple[ReferenceFeatureData, ...],
        *,
        candidate_count: int,
    ) -> int:
        if len(features) != 4 or candidate_count != 20:
            raise AssertionError("synthetic dry run shape changed")
        self._dry_run_calls.append((model.model_name, candidate_count))
        return 900 if "campplus" in model.model_name else 1_100


class SequencedPresenceAdapter:
    def __init__(
        self,
        scores: tuple[tuple[float, float], ...],
    ) -> None:
        if len(scores) != 20 or any(len(item) != 2 for item in scores):
            raise AssertionError("synthetic proposal sequence changed")
        self._scores = scores
        self.calls: list[AdapterRequest] = []

    def score(self, request: AdapterRequest) -> AdapterResponse:
        index = len(self.calls)
        if index >= len(self._scores):
            raise AssertionError("unexpected synthetic adapter call")
        self.calls.append(request)
        segments = []
        for ordinal, score in enumerate(self._scores[index], start=1):
            start_ms = (ordinal - 1) * 900
            end_ms = ordinal * 900
            segments.append(
                {
                    "end_ms": end_ms,
                    "evidence_hash": sha256_text(
                        canonical_json(
                            {
                                "audio_sha256": request.audio_sha256,
                                "end_ms": end_ms,
                                "ordinal": ordinal,
                                "raw_score": score,
                                "start_ms": start_ms,
                            }
                        )
                    ),
                    "ordinal": ordinal,
                    "raw_score": score,
                    "start_ms": start_ms,
                }
            )
        maximum = max(self._scores[index])
        if maximum >= request.subject_boundary:
            proposal = VoiceProposal.LIKELY_PRESENT
        elif maximum <= request.interviewer_boundary:
            proposal = VoiceProposal.LIKELY_ABSENT
        else:
            proposal = VoiceProposal.NEEDS_REVIEW
        values: dict[str, object] = {
            "adapter_contract_version": request.adapter_contract_version,
            "input_hash": request.input_hash,
            "model_name": request.model_name,
            "model_version": request.model_version,
            "proposal": proposal.value,
            "segments": segments,
            "vad_contract_version": request.vad_contract_version,
        }
        values["output_hash"] = sha256_text(canonical_json(values))
        return AdapterResponse.model_validate_json(
            canonical_json(values), strict=True
        )


def _write(path: Path, contents: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


def _synthetic_pcm_wav(*, duration_ms: int = 2_000) -> bytes:
    frame_count = 16_000 * duration_ms // 1_000
    pcm = b"\x00\x00" * frame_count
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        16_000,
        32_000,
        2,
        16,
        b"data",
        len(pcm),
    ) + pcm


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
    import_root = runtime_root / "Lib" / "site-packages"
    import_files = tuple(
        (
            relative,
            _write(import_root / Path(relative), contents),
        )
        for relative, contents in (
            (
                "market_voice_forecast_ledger/__init__.py",
                b"# synthetic installed project\n",
            ),
            (
                "market_voice_forecast_ledger/voice/adapter_main.py",
                b"# synthetic installed adapter\n",
            ),
            ("sherpa_onnx/__init__.py", b"# synthetic installed sherpa\n"),
        )
    )
    manifest = runtime_root / "startup-manifest.json"
    manifest_sha256 = _write(
        manifest,
        json.dumps(
            {
                "files": [
                    {"path": relative, "sha256": digest}
                    for relative, digest in import_files
                ]
            },
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    pyvenv = runtime_root / "pyvenv.cfg"
    pyvenv_sha256 = _write(
        pyvenv,
        b"home = C:/synthetic-python\n"
        b"include-system-site-packages = false\n"
        b"version = 3.14.6\n",
    )
    return (
        RuntimeAttestation(
            python_path=python.resolve(),
            python_sha256=hashes["python"],
            python_version="3.14.6",
            python_import_root=import_root.resolve(),
            python_import_files=import_files,
            python_startup_manifest_path=manifest.resolve(),
            python_startup_manifest_sha256=manifest_sha256,
            python_pyvenv_path=pyvenv.resolve(),
            python_pyvenv_sha256=pyvenv_sha256,
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
        write_before_error: bytes | None = None,
        extra_name: str | None = None,
        after_call: Any = None,
    ) -> None:
        self.output = output
        self.returncode = returncode
        self.error = error
        self.write_before_error = write_before_error
        self.extra_name = extra_name
        self.after_call = after_call
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> FakeCompleted:
        self.calls.append(tuple(argv))
        self.kwargs.append(dict(kwargs))
        output_path = (
            Path(argv[argv.index("-o") + 1])
            if "-o" in argv
            else Path(argv[-1])
        )
        if self.write_before_error is not None:
            output_path.write_bytes(self.write_before_error)
        if self.error is not None:
            raise self.error
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
    audio.write_bytes(_synthetic_pcm_wav())
    feature = b"synthetic-reference-feature"
    return AdapterRequest.with_canonical_hash(
        adapter_contract_version=attestation.adapter_contract_version,
        audio_duration_ms=2_000,
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
                "evidence_hash": sha256_text(
                    canonical_json(
                        {
                            "audio_sha256": request.audio_sha256,
                            "end_ms": 1000,
                            "ordinal": 1,
                            "raw_score": 0.9,
                            "start_ms": 0,
                        }
                    )
                ),
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
        after_call: Any = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.after_call = after_call
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
        if self.after_call is not None:
            self.after_call()
        return FakeCompleted(returncode=self.returncode, stdout=stdout)


def load_request(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    assert type(value) is dict
    return value
