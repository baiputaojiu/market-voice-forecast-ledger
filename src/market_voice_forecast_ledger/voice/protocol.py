"""Strict stdin/stdout contract for the isolated voice adapter."""

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import VoiceProposal


MAX_ADAPTER_RESPONSE_BYTES = 1_048_576
MAX_ADAPTER_SEGMENTS = 10_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SQLITE_INT_MAX = 2**63 - 1
_REFERENCE_ENCODING = "sherpa-speaker-embedding-v1"
_REFERENCE_DTYPE = "float32-le"
_REFERENCE_DIMENSIONS = {
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx": 192,
    "wespeaker_zh_cnceleb_resnet34.onnx": 256,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AdapterRequest(_StrictModel):
    adapter_contract_version: str
    audio_duration_ms: int
    audio_path: str
    audio_sha256: str
    interviewer_boundary: float
    input_hash: str
    model_name: str
    model_path: str
    model_sha256: str
    model_version: str
    reference_feature_b64: str
    reference_feature_length: int
    reference_feature_sha256: str
    subject_boundary: float
    threshold_config_version: str
    vad_contract_version: str
    vad_model_path: str
    vad_model_sha256: str

    @model_validator(mode="after")
    def _validate_shape(self) -> "AdapterRequest":
        if (
            not _safe_token(self.adapter_contract_version)
            or type(self.audio_duration_ms) is not int
            or not 1 <= self.audio_duration_ms <= 2_147_483_647
            or not _safe_token(self.model_name)
            or not _safe_token(self.model_version)
            or not _safe_token(self.threshold_config_version)
            or not _safe_token(self.vad_contract_version)
            or not _absolute_resolved_path(self.audio_path)
            or not _absolute_resolved_path(self.model_path)
            or not _absolute_resolved_path(self.vad_model_path)
            or not all(
                _sha256(value)
                for value in (
                    self.audio_sha256,
                    self.input_hash,
                    self.model_sha256,
                    self.reference_feature_sha256,
                    self.vad_model_sha256,
                )
            )
            or type(self.reference_feature_length) is not int
            or not 1 <= self.reference_feature_length <= MAX_ADAPTER_RESPONSE_BYTES
            or not _finite_float(self.subject_boundary)
            or not _finite_float(self.interviewer_boundary)
            or self.subject_boundary <= self.interviewer_boundary
        ):
            raise ValueError("invalid adapter request")
        return self

    @classmethod
    def with_canonical_hash(cls, **values: object) -> "AdapterRequest":
        feature: bytearray | None = None
        try:
            input_values = dict(values)
            input_values.pop("input_hash", None)
            request = cls(input_hash="0" * 64, **input_values)
            feature = _validated_feature(request)
            return request.model_copy(
                update={"input_hash": sha256_text(_request_canonical_json(request))}
            )
        except (TypeError, ValidationError, ValueError):
            raise _request_invalid() from None
        finally:
            if feature is not None:
                wipe_reference_feature(feature)


class AdapterSegment(_StrictModel):
    end_ms: int
    evidence_hash: str
    ordinal: int
    raw_score: float
    start_ms: int

    @model_validator(mode="after")
    def _validate_range(self) -> "AdapterSegment":
        if (
            type(self.ordinal) is not int
            or type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or self.ordinal < 1
            or self.start_ms < 0
            or self.end_ms <= self.start_ms
            or not _finite_float(self.raw_score)
            or not _sha256(self.evidence_hash)
        ):
            raise ValueError("invalid adapter segment")
        return self


class AdapterResponse(_StrictModel):
    adapter_contract_version: str
    input_hash: str
    model_name: str
    model_version: str
    output_hash: str
    proposal: VoiceProposal
    segments: tuple[AdapterSegment, ...]
    vad_contract_version: str

    @model_validator(mode="after")
    def _validate_shape(self) -> "AdapterResponse":
        if (
            not _safe_token(self.adapter_contract_version)
            or not _safe_token(self.model_name)
            or not _safe_token(self.model_version)
            or not _safe_token(self.vad_contract_version)
            or not _sha256(self.input_hash)
            or not _sha256(self.output_hash)
            or not self.segments
            or len(self.segments) > MAX_ADAPTER_SEGMENTS
        ):
            raise ValueError("invalid adapter response")
        previous_end = 0
        for ordinal, segment in enumerate(self.segments, start=1):
            if segment.ordinal != ordinal or segment.start_ms < previous_end:
                raise ValueError("invalid adapter segment order")
            previous_end = segment.end_ms
        return self


class ReferenceAudioInput(_StrictModel):
    approval_hash: str
    audio_duration_ms: int
    audio_path: str
    audio_sha256: str
    clip_kind: Literal["enrollment", "held_out_positive", "negative"]
    end_ms: int
    ordinal: int
    start_ms: int
    subject_id: int
    video_id: int

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceAudioInput":
        if (
            not _sha256(self.approval_hash)
            or not _sha256(self.audio_sha256)
            or not _absolute_resolved_path(self.audio_path)
            or not _positive_sqlite_int(self.audio_duration_ms)
            or not _positive_sqlite_int(self.ordinal)
            or self.ordinal > 6
            or not _positive_sqlite_int(self.subject_id)
            or not _positive_sqlite_int(self.video_id)
            or type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms <= self.audio_duration_ms
            or not 3_000 <= self.end_ms - self.start_ms <= 120_000
        ):
            raise ValueError("invalid reference audio")
        return self


class ReferenceFeatureInput(_StrictModel):
    dimension: int
    embedding_b64: str
    encoding_version: str
    feature_length: int
    feature_sha256: str
    float_dtype: str
    subject_id: int

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceFeatureInput":
        if (
            self.encoding_version != _REFERENCE_ENCODING
            or self.float_dtype != _REFERENCE_DTYPE
            or type(self.dimension) is not int
            or self.dimension not in set(_REFERENCE_DIMENSIONS.values())
            or type(self.feature_length) is not int
            or self.feature_length != self.dimension * 4
            or not _sha256(self.feature_sha256)
            or not _positive_sqlite_int(self.subject_id)
        ):
            raise ValueError("invalid reference feature")
        _decode_feature_bytes(
            self.embedding_b64,
            self.feature_length,
            self.feature_sha256,
        )
        return self

    @classmethod
    def from_bytes(
        cls,
        *,
        encoding_version: str,
        float_dtype: str,
        dimension: int,
        embedding_blob: bytes,
        subject_id: int,
    ) -> "ReferenceFeatureInput":
        try:
            if type(embedding_blob) is not bytes:
                raise ValueError("invalid reference feature bytes")
            return cls(
                dimension=dimension,
                embedding_b64=base64.b64encode(embedding_blob).decode("ascii"),
                encoding_version=encoding_version,
                feature_length=len(embedding_blob),
                feature_sha256=hashlib.sha256(embedding_blob).hexdigest(),
                float_dtype=float_dtype,
                subject_id=subject_id,
            )
        except (TypeError, ValidationError, ValueError):
            raise _reference_request_invalid() from None


class _ReferenceRequest(_StrictModel):
    adapter_contract_version: str
    input_hash: str
    model_name: str
    model_path: str
    model_sha256: str
    model_version: str

    def _validate_identity(self) -> None:
        if (
            not _safe_token(self.adapter_contract_version)
            or self.model_name not in _REFERENCE_DIMENSIONS
            or not _absolute_resolved_path(self.model_path)
            or not _sha256(self.model_sha256)
            or not _safe_token(self.model_version)
            or not _sha256(self.input_hash)
        ):
            raise ValueError("invalid reference request identity")

    @classmethod
    def with_canonical_hash(cls, **values: object) -> "_ReferenceRequest":
        try:
            inputs = dict(values)
            inputs.pop("input_hash", None)
            request = cls(input_hash="0" * 64, **inputs)
            return request.model_copy(
                update={"input_hash": _reference_request_hash(request)}
            )
        except (TypeError, ValidationError, ValueError):
            raise _reference_request_invalid() from None


class ReferenceEnrollmentRequest(_ReferenceRequest):
    operation: Literal["reference_enrollment"]
    audios: tuple[ReferenceAudioInput, ReferenceAudioInput]

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceEnrollmentRequest":
        self._validate_identity()
        if (
            type(self.audios) is not tuple
            or len(self.audios) != 2
            or tuple(item.ordinal for item in self.audios) != (1, 2)
            or any(item.clip_kind != "enrollment" for item in self.audios)
            or len({item.subject_id for item in self.audios}) != 1
        ):
            raise ValueError("invalid enrollment request")
        return self


class ReferenceScoreRequest(_ReferenceRequest):
    operation: Literal["reference_score"]
    audio: ReferenceAudioInput
    feature: ReferenceFeatureInput

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceScoreRequest":
        self._validate_identity()
        if (
            self.audio.clip_kind not in {"held_out_positive", "negative"}
            or self.audio.subject_id != self.feature.subject_id
            or self.feature.dimension != reference_feature_dimension(self.model_name)
        ):
            raise ValueError("invalid score request")
        return self


class ReferenceDryRunRequest(_ReferenceRequest):
    operation: Literal["reference_dry_run"]
    audios: tuple[ReferenceAudioInput, ...]
    candidate_count: int
    features: tuple[ReferenceFeatureInput, ...]

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceDryRunRequest":
        self._validate_identity()
        if (
            self.candidate_count != 20
            or type(self.audios) is not tuple
            or len(self.audios) != 20
            or type(self.features) is not tuple
            or len(self.features) != 4
            or tuple(item.subject_id for item in self.features)
            != tuple(sorted(item.subject_id for item in self.features))
            or len({item.subject_id for item in self.features}) != 4
            or any(
                item.dimension != reference_feature_dimension(self.model_name)
                for item in self.features
            )
        ):
            raise ValueError("invalid dry-run request")
        return self


ReferenceRequest = (
    ReferenceEnrollmentRequest | ReferenceScoreRequest | ReferenceDryRunRequest
)


class _ReferenceResponse(_StrictModel):
    adapter_contract_version: str
    cpu_time_ms: int
    input_hash: str
    model_name: str
    model_version: str
    output_hash: str

    def _validate_identity(self) -> None:
        if (
            not _safe_token(self.adapter_contract_version)
            or type(self.cpu_time_ms) is not int
            or not 0 <= self.cpu_time_ms <= _SQLITE_INT_MAX
            or not _sha256(self.input_hash)
            or self.model_name not in _REFERENCE_DIMENSIONS
            or not _safe_token(self.model_version)
            or not _sha256(self.output_hash)
        ):
            raise ValueError("invalid reference response identity")


class ReferenceEnrollmentResponse(_ReferenceResponse):
    operation: Literal["reference_enrollment"]
    dimension: int
    encoding_version: str
    feature_b64: str
    feature_length: int
    feature_sha256: str
    float_dtype: str

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceEnrollmentResponse":
        self._validate_identity()
        if (
            self.encoding_version != _REFERENCE_ENCODING
            or self.float_dtype != _REFERENCE_DTYPE
            or self.dimension != reference_feature_dimension(self.model_name)
            or self.feature_length != self.dimension * 4
            or not _sha256(self.feature_sha256)
        ):
            raise ValueError("invalid enrollment response")
        _decode_feature_bytes(
            self.feature_b64, self.feature_length, self.feature_sha256
        )
        return self


class ReferenceScoreResponse(_ReferenceResponse):
    operation: Literal["reference_score"]
    raw_score: float

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceScoreResponse":
        self._validate_identity()
        if not _cosine_float(self.raw_score):
            raise ValueError("invalid reference score")
        return self


class ReferenceDryRunResponse(_ReferenceResponse):
    operation: Literal["reference_dry_run"]
    candidate_count: int

    @model_validator(mode="after")
    def _validate_shape(self) -> "ReferenceDryRunResponse":
        self._validate_identity()
        if self.candidate_count != 20:
            raise ValueError("invalid dry-run response")
        return self


ReferenceResponse = (
    ReferenceEnrollmentResponse | ReferenceScoreResponse | ReferenceDryRunResponse
)


def encode_request(request: AdapterRequest) -> bytes:
    feature: bytearray | None = None
    if not isinstance(request, AdapterRequest):
        raise _request_invalid()
    try:
        feature = _validated_feature(request)
        if request.input_hash != sha256_text(_request_canonical_json(request)):
            raise ValueError("request hash mismatch")
        return canonical_json(request.model_dump(mode="json")).encode("utf-8")
    except (TypeError, ValidationError, ValueError):
        raise _request_invalid() from None
    finally:
        if feature is not None:
            wipe_reference_feature(feature)


def decode_reference_feature(request: AdapterRequest, *, max_bytes: int) -> bytearray:
    if type(max_bytes) is not int or max_bytes < 1:
        raise _request_invalid()
    feature: bytearray | None = None
    succeeded = False
    try:
        feature = _validated_feature(request)
        if len(feature) != max_bytes:
            raise ValueError("reference feature length mismatch")
        succeeded = True
        return feature
    except (TypeError, ValidationError, ValueError):
        raise _request_invalid() from None
    finally:
        if feature is not None and not succeeded:
            wipe_reference_feature(feature)


def wipe_reference_feature(feature: bytearray) -> None:
    if type(feature) is not bytearray:
        raise _request_invalid()
    for index in range(len(feature)):
        feature[index] = 0


def decode_response(payload: object, *, expected_request: AdapterRequest) -> AdapterResponse:
    try:
        if not isinstance(expected_request, AdapterRequest):
            raise ValueError("invalid expected request")
        if type(payload) is not bytes or len(payload) > MAX_ADAPTER_RESPONSE_BYTES:
            raise ValueError("invalid response bytes")
        value = json.loads(
            payload.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object
        )
        if type(value) is not dict:
            raise ValueError("response is not an object")
        response = AdapterResponse.model_validate_json(canonical_json(value), strict=True)
        if (
            response.input_hash != expected_request.input_hash
            or response.model_name != expected_request.model_name
            or response.model_version != expected_request.model_version
            or response.adapter_contract_version != expected_request.adapter_contract_version
            or response.vad_contract_version != expected_request.vad_contract_version
        ):
            raise ValueError("adapter identity mismatch")
        expected_hash = sha256_text(
            canonical_json(
                {
                    key: item
                    for key, item in response.model_dump(mode="json").items()
                    if key != "output_hash"
                }
            )
        )
        if response.output_hash != expected_hash:
            raise ValueError("adapter output hash mismatch")
        for segment in response.segments:
            if segment.end_ms > expected_request.audio_duration_ms:
                raise ValueError("adapter segment exceeds audio")
            expected_evidence = sha256_text(
                canonical_json(
                    {
                        "audio_sha256": expected_request.audio_sha256,
                        "end_ms": segment.end_ms,
                        "ordinal": segment.ordinal,
                        "raw_score": segment.raw_score,
                        "start_ms": segment.start_ms,
                    }
                )
            )
            if segment.evidence_hash != expected_evidence:
                raise ValueError("adapter evidence hash mismatch")
        maximum = max(segment.raw_score for segment in response.segments)
        if maximum >= expected_request.subject_boundary:
            expected_proposal = VoiceProposal.LIKELY_PRESENT
        elif maximum <= expected_request.interviewer_boundary:
            expected_proposal = VoiceProposal.LIKELY_ABSENT
        else:
            expected_proposal = VoiceProposal.NEEDS_REVIEW
        if response.proposal is not expected_proposal:
            raise ValueError("adapter proposal mismatch")
        return response
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        raise _response_invalid() from None


def encode_reference_request(request: ReferenceRequest) -> bytes:
    try:
        if not isinstance(
            request,
            (
                ReferenceEnrollmentRequest,
                ReferenceScoreRequest,
                ReferenceDryRunRequest,
            ),
        ):
            raise ValueError("invalid reference request")
        validated = type(request).model_validate(request, strict=True)
        if validated.input_hash != _reference_request_hash(validated):
            raise ValueError("reference request hash mismatch")
        return canonical_json(validated.model_dump(mode="json")).encode("utf-8")
    except (TypeError, ValidationError, ValueError):
        raise _reference_request_invalid() from None


def decode_reference_request(payload: object) -> ReferenceRequest:
    try:
        value = _decode_unique_payload(payload)
        operation = value.get("operation")
        request_type = {
            "reference_enrollment": ReferenceEnrollmentRequest,
            "reference_score": ReferenceScoreRequest,
            "reference_dry_run": ReferenceDryRunRequest,
        }.get(operation)
        if request_type is None:
            raise ValueError("unknown reference operation")
        request = request_type.model_validate_json(
            canonical_json(value), strict=True
        )
        if request.input_hash != _reference_request_hash(request):
            raise ValueError("reference request hash mismatch")
        if encode_reference_request(request) != payload:
            raise ValueError("reference request is not canonical")
        return request
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        raise _reference_request_invalid() from None


def encode_reference_response(response: ReferenceResponse) -> bytes:
    try:
        if not isinstance(
            response,
            (
                ReferenceEnrollmentResponse,
                ReferenceScoreResponse,
                ReferenceDryRunResponse,
            ),
        ):
            raise ValueError("invalid reference response")
        validated = type(response).model_validate(response, strict=True)
        if validated.output_hash != _reference_response_hash(validated):
            raise ValueError("reference response hash mismatch")
        return canonical_json(validated.model_dump(mode="json")).encode("utf-8")
    except (TypeError, ValidationError, ValueError):
        raise _reference_response_invalid() from None


def decode_reference_response(
    payload: object,
    *,
    expected_request: ReferenceRequest,
) -> ReferenceResponse:
    try:
        if not isinstance(
            expected_request,
            (
                ReferenceEnrollmentRequest,
                ReferenceScoreRequest,
                ReferenceDryRunRequest,
            ),
        ):
            raise ValueError("invalid expected reference request")
        value = _decode_unique_payload(payload)
        operation = value.get("operation")
        response_type = {
            "reference_enrollment": ReferenceEnrollmentResponse,
            "reference_score": ReferenceScoreResponse,
            "reference_dry_run": ReferenceDryRunResponse,
        }.get(operation)
        if response_type is None:
            raise ValueError("unknown reference response operation")
        response = response_type.model_validate_json(
            canonical_json(value), strict=True
        )
        if (
            response.operation != expected_request.operation
            or response.input_hash != expected_request.input_hash
            or response.model_name != expected_request.model_name
            or response.model_version != expected_request.model_version
            or response.adapter_contract_version
            != expected_request.adapter_contract_version
            or response.output_hash != _reference_response_hash(response)
        ):
            raise ValueError("reference response identity mismatch")
        if isinstance(response, ReferenceDryRunResponse) and (
            not isinstance(expected_request, ReferenceDryRunRequest)
            or response.candidate_count != expected_request.candidate_count
        ):
            raise ValueError("dry-run response mismatch")
        return response
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        raise _reference_response_invalid() from None


def decode_reference_input_feature(feature: ReferenceFeatureInput) -> bytearray:
    try:
        if not isinstance(feature, ReferenceFeatureInput):
            raise ValueError("invalid reference feature")
        return _decode_feature_bytes(
            feature.embedding_b64,
            feature.feature_length,
            feature.feature_sha256,
        )
    except (TypeError, ValidationError, ValueError):
        raise _reference_request_invalid() from None


def reference_feature_dimension(model_name: object) -> int:
    if type(model_name) is not str or model_name not in _REFERENCE_DIMENSIONS:
        raise ValueError("unsupported reference model")
    return _REFERENCE_DIMENSIONS[model_name]


def reference_feature_semantics(model_name: object) -> tuple[str, str, int]:
    return (
        _REFERENCE_ENCODING,
        _REFERENCE_DTYPE,
        reference_feature_dimension(model_name),
    )


def _request_canonical_json(request: AdapterRequest) -> str:
    return canonical_json(
        {
            key: value
            for key, value in request.model_dump(mode="json").items()
            if key != "input_hash"
        }
    )


def _reference_request_hash(request: ReferenceRequest) -> str:
    return sha256_text(
        canonical_json(
            {
                key: value
                for key, value in request.model_dump(mode="json").items()
                if key != "input_hash"
            }
        )
    )


def _reference_response_hash(response: ReferenceResponse) -> str:
    return sha256_text(
        canonical_json(
            {
                key: value
                for key, value in response.model_dump(mode="json").items()
                if key != "output_hash"
            }
        )
    )


def _decode_unique_payload(payload: object) -> dict[str, object]:
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > MAX_ADAPTER_RESPONSE_BYTES
    ):
        raise ValueError("invalid reference payload")
    value = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
    )
    if type(value) is not dict:
        raise ValueError("reference payload is not an object")
    return value


def _decode_feature_bytes(
    encoded: object, expected_length: object, expected_sha256: object
) -> bytearray:
    if (
        type(encoded) is not str
        or type(expected_length) is not int
        or expected_length < 1
        or expected_length > MAX_ADAPTER_RESPONSE_BYTES
        or not _sha256(expected_sha256)
        or not encoded.isascii()
        or len(encoded) != 4 * ((expected_length + 2) // 3)
    ):
        raise ValueError("invalid reference feature encoding")
    decoded = bytearray(base64.b64decode(encoded, validate=True))
    if (
        len(decoded) != expected_length
        or hashlib.sha256(decoded).hexdigest() != expected_sha256
    ):
        wipe_reference_feature(decoded)
        raise ValueError("invalid reference feature identity")
    return decoded


def _validated_feature(request: AdapterRequest) -> bytearray:
    if not isinstance(request, AdapterRequest):
        raise ValueError("invalid request")
    encoded = request.reference_feature_b64
    expected_length = request.reference_feature_length
    if (
        type(encoded) is not str
        or type(expected_length) is not int
        or expected_length < 1
        or expected_length > MAX_ADAPTER_RESPONSE_BYTES
        or not encoded.isascii()
        or len(encoded) != 4 * ((expected_length + 2) // 3)
    ):
        raise ValueError("invalid feature encoding")
    decoded: bytearray | None = None
    succeeded = False
    try:
        decoded = bytearray(base64.b64decode(encoded, validate=True))
        if (
            len(decoded) != expected_length
            or hashlib.sha256(decoded).hexdigest() != request.reference_feature_sha256
        ):
            raise ValueError("invalid feature identity")
        succeeded = True
        return decoded
    except (binascii.Error, ValueError):
        raise ValueError("invalid feature encoding") from None
    finally:
        if decoded is not None and not succeeded:
            wipe_reference_feature(decoded)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_token(value: object) -> bool:
    return type(value) is str and _SAFE_TOKEN.fullmatch(value) is not None


def _absolute_resolved_path(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= 4_096
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and _is_absolute_resolved(value)
    )


def _is_absolute_resolved(value: str) -> bool:
    from pathlib import Path

    path = Path(value)
    return path.is_absolute() and path == path.resolve(strict=False)


def _sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _finite_float(value: object) -> bool:
    return type(value) is float and math.isfinite(value)


def _cosine_float(value: object) -> bool:
    return _finite_float(value) and -1.0 <= value <= 1.0


def _positive_sqlite_int(value: object) -> bool:
    return type(value) is int and 0 < value <= _SQLITE_INT_MAX


def _request_invalid() -> DomainError:
    return DomainError("VOICE_ADAPTER_REQUEST_INVALID", "adapter request is invalid")


def _response_invalid() -> DomainError:
    return DomainError("VOICE_ADAPTER_RESPONSE_INVALID", "adapter response is invalid")


def _reference_request_invalid() -> DomainError:
    return DomainError(
        "VOICE_REFERENCE_ADAPTER_REQUEST_INVALID",
        "reference adapter request is invalid",
    )


def _reference_response_invalid() -> DomainError:
    return DomainError(
        "VOICE_REFERENCE_ADAPTER_RESPONSE_INVALID",
        "reference adapter response is invalid",
    )


__all__ = [
    "AdapterRequest",
    "AdapterResponse",
    "AdapterSegment",
    "MAX_ADAPTER_RESPONSE_BYTES",
    "MAX_ADAPTER_SEGMENTS",
    "ReferenceAudioInput",
    "ReferenceDryRunRequest",
    "ReferenceDryRunResponse",
    "ReferenceEnrollmentRequest",
    "ReferenceEnrollmentResponse",
    "ReferenceFeatureInput",
    "ReferenceRequest",
    "ReferenceResponse",
    "ReferenceScoreRequest",
    "ReferenceScoreResponse",
    "decode_reference_input_feature",
    "decode_reference_request",
    "decode_reference_response",
    "decode_reference_feature",
    "decode_response",
    "encode_reference_request",
    "encode_reference_response",
    "encode_request",
    "reference_feature_dimension",
    "reference_feature_semantics",
    "wipe_reference_feature",
]
