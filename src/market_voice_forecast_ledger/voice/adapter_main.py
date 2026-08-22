"""Network-denied entrypoint for CPU-only local speaker scoring."""

import base64
import hashlib
import json
import math
import _socket
import socket
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import VoiceProposal
from market_voice_forecast_ledger.voice.protocol import (
    MAX_ADAPTER_RESPONSE_BYTES,
    MAX_ADAPTER_SEGMENTS,
    AdapterRequest,
    AdapterResponse,
    ReferenceAudioInput,
    ReferenceDryRunRequest,
    ReferenceDryRunResponse,
    ReferenceEnrollmentRequest,
    ReferenceEnrollmentResponse,
    ReferenceRequest,
    ReferenceResponse,
    ReferenceScoreRequest,
    ReferenceScoreResponse,
    decode_reference_input_feature,
    decode_reference_request,
    decode_reference_response,
    decode_reference_feature,
    decode_response,
    encode_reference_response,
    encode_request,
    reference_feature_semantics,
    wipe_reference_feature,
)


MAX_ADAPTER_REQUEST_BYTES = 2_097_152


class _Backend(Protocol):
    def score(self) -> AdapterResponse: ...


BackendFactory = Callable[[AdapterRequest, bytearray], _Backend]
ReferenceBackend = Callable[[ReferenceRequest], ReferenceResponse]


def process_payload(
    payload: bytes,
    *,
    backend_factory: BackendFactory | None = None,
    socket_module: Any = None,
    low_level_socket_module: Any = None,
) -> bytes:
    feature: bytearray | None = None
    try:
        request = _decode_request(payload)
        if socket_module is None:
            socket_module = socket
        if low_level_socket_module is None:
            low_level_socket_module = _socket

        _install_network_denial(socket_module, low_level_socket_module)
        feature = decode_reference_feature(
            request, max_bytes=request.reference_feature_length
        )
        try:
            backend = (backend_factory or _create_backend)(request, feature)
        finally:
            wipe_reference_feature(feature)
        response = backend.score()
        if not isinstance(response, AdapterResponse):
            raise ValueError("invalid adapter backend response")
        encoded = canonical_json(response.model_dump(mode="json")).encode("utf-8")
        canonical = decode_response(encoded, expected_request=request)
        return canonical_json(canonical.model_dump(mode="json")).encode("utf-8")
    except Exception:
        raise _process_failed() from None
    finally:
        if feature is not None and any(feature):
            wipe_reference_feature(feature)


def process_reference_payload(
    payload: bytes,
    *,
    backend: ReferenceBackend | None = None,
    socket_module: Any = None,
    low_level_socket_module: Any = None,
) -> bytes:
    try:
        request = decode_reference_request(payload)
        if socket_module is None:
            socket_module = socket
        if low_level_socket_module is None:
            low_level_socket_module = _socket
        _install_network_denial(socket_module, low_level_socket_module)
        response = (backend or _execute_reference_request)(request)
        encoded = encode_reference_response(response)
        canonical = decode_reference_response(
            encoded, expected_request=request
        )
        return encode_reference_response(canonical)
    except Exception:
        raise DomainError(
            "VOICE_REFERENCE_ADAPTER_PROCESS_FAILED",
            "reference adapter process failed",
        ) from None


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(MAX_ADAPTER_REQUEST_BYTES + 1)
        value = json.loads(payload.decode("utf-8", errors="strict"))
        output = (
            process_reference_payload(payload)
            if type(value) is dict
            and value.get("operation")
            in {
                "reference_enrollment",
                "reference_score",
                "reference_dry_run",
            }
            else process_payload(payload)
        )
        if len(output) > MAX_ADAPTER_RESPONSE_BYTES:
            raise ValueError("adapter output is oversized")
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


def _decode_request(payload: object) -> AdapterRequest:
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > MAX_ADAPTER_REQUEST_BYTES
    ):
        raise ValueError("invalid adapter request bytes")
    value = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
    )
    if type(value) is not dict:
        raise ValueError("adapter request is not an object")
    request = AdapterRequest.model_validate_json(canonical_json(value), strict=True)
    if encode_request(request) != payload:
        raise ValueError("adapter request is not canonical")
    return request


def _install_network_denial(
    socket_module: Any, low_level_socket_module: Any
) -> None:
    def _network_disabled(*args: object, **kwargs: object) -> object:
        raise OSError("network disabled")

    for name in (
        "socket",
        "SocketType",
        "create_connection",
        "getaddrinfo",
        "socketpair",
        "fromfd",
        "fromshare",
    ):
        setattr(socket_module, name, _network_disabled)
    for name in ("socket", "socketpair"):
        setattr(low_level_socket_module, name, _network_disabled)


def _create_backend(request: AdapterRequest, feature: bytearray) -> _Backend:
    import importlib
    import wave
    from array import array

    _require_file_hash(Path(request.audio_path), request.audio_sha256)
    _require_file_hash(Path(request.model_path), request.model_sha256)
    _require_file_hash(Path(request.vad_model_path), request.vad_model_sha256)
    reference = array("f")
    reference.frombytes(feature)
    if sys.byteorder != "little":
        reference.byteswap()
    if not reference or any(not math.isfinite(value) for value in reference):
        raise ValueError("invalid reference feature")
    with wave.open(request.audio_path, "rb") as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getframerate() != 16_000
            or wav.getcomptype() != "NONE"
        ):
            raise ValueError("invalid normalized audio")
        if (wav.getnframes() * 1_000) // 16_000 != request.audio_duration_ms:
            raise ValueError("normalized audio duration changed")
        pcm = array("h")
        pcm.frombytes(wav.readframes(wav.getnframes()))
    if sys.byteorder != "little":
        pcm.byteswap()
    if not pcm:
        raise ValueError("normalized audio is empty")
    samples = tuple(value / 32_768.0 for value in pcm)
    sherpa = importlib.import_module("sherpa_onnx")
    return _SherpaBackend.initialize(
        sherpa=sherpa,
        request=request,
        samples=samples,
        reference=tuple(float(value) for value in reference),
    )


def _execute_reference_request(request: ReferenceRequest) -> ReferenceResponse:
    started = time.process_time_ns()
    runtime = _ReferenceEmbeddingRuntime.initialize(request)
    if isinstance(request, ReferenceEnrollmentRequest):
        embeddings = tuple(runtime.embedding(item) for item in request.audios)
        feature = _normalized_average(embeddings)
        feature_blob = struct.pack(f"<{len(feature)}f", *feature)
        encoding, float_dtype, dimension = reference_feature_semantics(
            request.model_name
        )
        values: dict[str, object] = {
            "adapter_contract_version": request.adapter_contract_version,
            "cpu_time_ms": _elapsed_cpu_ms(started),
            "dimension": dimension,
            "encoding_version": encoding,
            "feature_b64": base64.b64encode(feature_blob).decode("ascii"),
            "feature_length": len(feature_blob),
            "feature_sha256": hashlib.sha256(feature_blob).hexdigest(),
            "float_dtype": float_dtype,
            "input_hash": request.input_hash,
            "model_name": request.model_name,
            "model_version": request.model_version,
            "operation": request.operation,
        }
        values["output_hash"] = sha256_text(canonical_json(values))
        return ReferenceEnrollmentResponse.model_validate(values, strict=True)
    if isinstance(request, ReferenceScoreRequest):
        feature = _reference_feature_values(request.feature)
        try:
            raw_score = _cosine(feature, runtime.embedding(request.audio))
        finally:
            feature = ()
        values = {
            "adapter_contract_version": request.adapter_contract_version,
            "cpu_time_ms": _elapsed_cpu_ms(started),
            "input_hash": request.input_hash,
            "model_name": request.model_name,
            "model_version": request.model_version,
            "operation": request.operation,
            "raw_score": raw_score,
        }
        values["output_hash"] = sha256_text(canonical_json(values))
        return ReferenceScoreResponse.model_validate(values, strict=True)
    if not isinstance(request, ReferenceDryRunRequest):
        raise ValueError("unsupported reference request")
    features = tuple(_reference_feature_values(item) for item in request.features)
    try:
        for audio in request.audios:
            embedding = runtime.embedding(audio)
            max(_cosine(feature, embedding) for feature in features)
    finally:
        features = ()
    values = {
        "adapter_contract_version": request.adapter_contract_version,
        "candidate_count": request.candidate_count,
        "cpu_time_ms": _elapsed_cpu_ms(started),
        "input_hash": request.input_hash,
        "model_name": request.model_name,
        "model_version": request.model_version,
        "operation": request.operation,
    }
    values["output_hash"] = sha256_text(canonical_json(values))
    return ReferenceDryRunResponse.model_validate(values, strict=True)


class _ReferenceEmbeddingRuntime:
    def __init__(self, extractor: Any) -> None:
        self._extractor = extractor

    @classmethod
    def initialize(
        cls, request: ReferenceRequest
    ) -> "_ReferenceEmbeddingRuntime":
        import importlib

        _require_file_hash(Path(request.model_path), request.model_sha256)
        sherpa = importlib.import_module("sherpa_onnx")
        config = sherpa.SpeakerEmbeddingExtractorConfig(
            model=request.model_path,
            num_threads=1,
            debug=False,
            provider="cpu",
        )
        extractor = sherpa.SpeakerEmbeddingExtractor(config)
        readiness = getattr(extractor, "is_ready", None)
        if callable(readiness) and not readiness():
            raise ValueError("speaker extractor is not ready")
        return cls(extractor)

    def embedding(self, audio: ReferenceAudioInput) -> tuple[float, ...]:
        import wave
        from array import array

        path = Path(audio.audio_path)
        _require_file_hash(path, audio.audio_sha256)
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 16_000
                or wav.getcomptype() != "NONE"
                or (wav.getnframes() * 1_000) // 16_000
                != audio.audio_duration_ms
            ):
                raise ValueError("invalid reference audio")
            start_frame = audio.start_ms * 16
            frame_count = (audio.end_ms - audio.start_ms) * 16
            if start_frame + frame_count > wav.getnframes():
                raise ValueError("reference range exceeds audio")
            wav.setpos(start_frame)
            pcm = array("h")
            pcm.frombytes(wav.readframes(frame_count))
        if sys.byteorder != "little":
            pcm.byteswap()
        if len(pcm) != frame_count:
            raise ValueError("reference range is incomplete")
        samples = tuple(value / 32_768.0 for value in pcm)
        stream = self._extractor.create_stream()
        stream.accept_waveform(16_000, samples)
        stream.input_finished()
        result = tuple(float(value) for value in self._extractor.compute(stream))
        if not result or any(not math.isfinite(value) for value in result):
            raise ValueError("invalid speaker embedding")
        return result


def _reference_feature_values(feature: Any) -> tuple[float, ...]:
    from array import array

    body = decode_reference_input_feature(feature)
    try:
        values = array("f")
        values.frombytes(body)
        if sys.byteorder != "little":
            values.byteswap()
        result = tuple(float(value) for value in values)
        if len(result) != feature.dimension or any(
            not math.isfinite(value) for value in result
        ):
            raise ValueError("invalid reference feature")
        return result
    finally:
        wipe_reference_feature(body)


def _normalized_average(
    embeddings: tuple[tuple[float, ...], ...]
) -> tuple[float, ...]:
    if not embeddings or len({len(item) for item in embeddings}) != 1:
        raise ValueError("embedding dimensions differ")
    averaged = tuple(
        sum(item[index] for item in embeddings) / len(embeddings)
        for index in range(len(embeddings[0]))
    )
    norm = math.sqrt(sum(value * value for value in averaged))
    if norm == 0.0 or not math.isfinite(norm):
        raise ValueError("enrollment embedding norm is zero")
    return tuple(value / norm for value in averaged)


def _elapsed_cpu_ms(started_ns: int) -> int:
    elapsed = time.process_time_ns() - started_ns
    if elapsed < 0:
        raise ValueError("child CPU clock moved backwards")
    return (elapsed + 999_999) // 1_000_000


class _SherpaBackend:
    def __init__(
        self,
        *,
        request: AdapterRequest,
        samples: tuple[float, ...],
        reference: tuple[float, ...],
        vad: Any,
        extractor: Any,
    ) -> None:
        self._request = request
        self._samples = samples
        self._reference = reference
        self._vad = vad
        self._extractor = extractor

    @classmethod
    def initialize(
        cls,
        *,
        sherpa: Any,
        request: AdapterRequest,
        samples: tuple[float, ...],
        reference: tuple[float, ...],
    ) -> "_SherpaBackend":
        extractor_config = sherpa.SpeakerEmbeddingExtractorConfig(
            model=request.model_path,
            num_threads=1,
            debug=False,
            provider="cpu",
        )
        extractor = sherpa.SpeakerEmbeddingExtractor(extractor_config)
        readiness = getattr(extractor, "is_ready", None)
        if callable(readiness) and not readiness():
            raise ValueError("speaker extractor is not ready")
        silero = sherpa.SileroVadModelConfig(
            model=request.vad_model_path,
            threshold=0.5,
            min_silence_duration=0.25,
            min_speech_duration=0.25,
            max_speech_duration=30.0,
            window_size=512,
        )
        vad_config = sherpa.VadModelConfig(
            silero_vad=silero,
            sample_rate=16_000,
            num_threads=1,
            debug=False,
            provider="cpu",
        )
        vad = sherpa.VoiceActivityDetector(
            vad_config, buffer_size_in_seconds=100
        )
        return cls(
            request=request,
            samples=samples,
            reference=reference,
            vad=vad,
            extractor=extractor,
        )

    def score(self) -> AdapterResponse:
        self._vad.accept_waveform(self._samples)
        self._vad.flush()
        scored: list[dict[str, object]] = []
        while not _vad_empty(self._vad):
            segment = _vad_front(self._vad)
            segment_samples = tuple(float(value) for value in segment.samples)
            start_sample = int(segment.start)
            self._vad.pop()
            if not segment_samples:
                continue
            embedding = self._embedding(segment_samples)
            raw_score = _cosine(self._reference, embedding)
            start_ms = (start_sample * 1_000) // 16_000
            end_ms = ((start_sample + len(segment_samples)) * 1_000) // 16_000
            if end_ms <= start_ms:
                continue
            ordinal = len(scored) + 1
            evidence_hash = sha256_text(
                canonical_json(
                    {
                        "audio_sha256": self._request.audio_sha256,
                        "end_ms": end_ms,
                        "ordinal": ordinal,
                        "raw_score": raw_score,
                        "start_ms": start_ms,
                    }
                )
            )
            scored.append(
                {
                    "end_ms": end_ms,
                    "evidence_hash": evidence_hash,
                    "ordinal": ordinal,
                    "raw_score": raw_score,
                    "start_ms": start_ms,
                }
            )
            if len(scored) > MAX_ADAPTER_SEGMENTS:
                raise ValueError("too many speech segments")
        if not scored:
            raise ValueError("speech is unavailable")
        maximum = max(float(item["raw_score"]) for item in scored)
        if maximum >= self._request.subject_boundary:
            proposal = VoiceProposal.LIKELY_PRESENT
        elif maximum <= self._request.interviewer_boundary:
            proposal = VoiceProposal.LIKELY_ABSENT
        else:
            proposal = VoiceProposal.NEEDS_REVIEW
        values: dict[str, object] = {
            "adapter_contract_version": self._request.adapter_contract_version,
            "input_hash": self._request.input_hash,
            "model_name": self._request.model_name,
            "model_version": self._request.model_version,
            "proposal": proposal.value,
            "segments": scored,
            "vad_contract_version": self._request.vad_contract_version,
        }
        values["output_hash"] = sha256_text(canonical_json(values))
        return AdapterResponse.model_validate_json(canonical_json(values), strict=True)

    def _embedding(self, samples: tuple[float, ...]) -> tuple[float, ...]:
        stream = self._extractor.create_stream()
        stream.accept_waveform(16_000, samples)
        stream.input_finished()
        result = tuple(float(value) for value in self._extractor.compute(stream))
        if not result or any(not math.isfinite(value) for value in result):
            raise ValueError("invalid speaker embedding")
        return result


def _vad_empty(vad: Any) -> bool:
    value = vad.empty
    return bool(value() if callable(value) else value)


def _vad_front(vad: Any) -> Any:
    value = vad.front
    return value() if callable(value) else value


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding dimension mismatch")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("embedding norm is zero")
    score = numerator / (left_norm * right_norm)
    if not math.isfinite(score):
        raise ValueError("speaker score is invalid")
    score = max(-1.0, min(1.0, score))
    return 0.0 if score == 0.0 else score


def _require_file_hash(path: Path, expected: str) -> None:
    if (
        not path.is_absolute()
        or path != path.resolve(strict=True)
        or not path.is_file()
    ):
        raise ValueError("adapter artifact is invalid")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65_536):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("adapter artifact changed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate adapter request key")
        result[key] = value
    return result


def _process_failed() -> DomainError:
    return DomainError(
        "VOICE_ADAPTER_PROCESS_FAILED", "voice adapter process failed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
