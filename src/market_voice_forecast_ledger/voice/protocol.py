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


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AdapterRequest(_StrictModel):
    adapter_contract_version: str
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
            or len(self.segments) > MAX_ADAPTER_SEGMENTS
        ):
            raise ValueError("invalid adapter response")
        previous_end = 0
        for ordinal, segment in enumerate(self.segments, start=1):
            if segment.ordinal != ordinal or segment.start_ms < previous_end:
                raise ValueError("invalid adapter segment order")
            previous_end = segment.end_ms
        return self


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
        return response
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        raise _response_invalid() from None


def _request_canonical_json(request: AdapterRequest) -> str:
    return canonical_json(
        {
            key: value
            for key, value in request.model_dump(mode="json").items()
            if key != "input_hash"
        }
    )


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


def _request_invalid() -> DomainError:
    return DomainError("VOICE_ADAPTER_REQUEST_INVALID", "adapter request is invalid")


def _response_invalid() -> DomainError:
    return DomainError("VOICE_ADAPTER_RESPONSE_INVALID", "adapter response is invalid")


__all__ = [
    "AdapterRequest",
    "AdapterResponse",
    "AdapterSegment",
    "MAX_ADAPTER_RESPONSE_BYTES",
    "MAX_ADAPTER_SEGMENTS",
    "decode_reference_feature",
    "decode_response",
    "encode_request",
    "wipe_reference_feature",
]
