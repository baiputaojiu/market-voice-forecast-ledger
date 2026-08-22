import base64
import hashlib
import json
import math

import pytest

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import VoiceProposal
from market_voice_forecast_ledger.voice.protocol import (
    AdapterResponse,
    AdapterSegment,
    MAX_ADAPTER_RESPONSE_BYTES,
    MAX_ADAPTER_SEGMENTS,
    AdapterRequest,
    decode_reference_feature,
    decode_response,
    encode_request,
    wipe_reference_feature,
)
from market_voice_forecast_ledger.voice import protocol


def _request() -> AdapterRequest:
    feature = b"reference-feature"
    values = {
        "adapter_contract_version": "voice-adapter-v1",
        "audio_path": "C:/private/work/audio.wav",
        "audio_sha256": "a" * 64,
        "interviewer_boundary": 0.2,
        "model_name": "model.onnx",
        "model_path": "C:/private/models/model.onnx",
        "model_sha256": "b" * 64,
        "model_version": "model-v1",
        "reference_feature_b64": base64.b64encode(feature).decode("ascii"),
        "reference_feature_length": len(feature),
        "reference_feature_sha256": hashlib.sha256(feature).hexdigest(),
        "subject_boundary": 0.8,
        "threshold_config_version": "threshold-v1",
        "vad_contract_version": "vad-v1",
        "vad_model_path": "C:/private/models/vad.onnx",
        "vad_model_sha256": "c" * 64,
    }
    return AdapterRequest.with_canonical_hash(**values)


def _response_payload(request: AdapterRequest) -> bytes:
    values = {
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
            },
            {
                "end_ms": 2000,
                "evidence_hash": "e" * 64,
                "ordinal": 2,
                "raw_score": 0.85,
                "start_ms": 1000,
            },
        ],
        "vad_contract_version": request.vad_contract_version,
    }
    values["output_hash"] = sha256_text(canonical_json(values))
    return canonical_json(values).encode("utf-8")


def _mutated_response(request: AdapterRequest, mutation: str) -> bytes:
    values = json.loads(_response_payload(request))
    if mutation == "unknown_field":
        values["unexpected"] = "value"
    elif mutation == "nan_score":
        values["segments"][0]["raw_score"] = math.nan
    elif mutation == "infinite_score":
        values["segments"][0]["raw_score"] = math.inf
    elif mutation == "noncontiguous":
        values["segments"][1]["ordinal"] = 3
    elif mutation == "reordered":
        values["segments"].reverse()
    elif mutation == "overlap":
        values["segments"][1]["start_ms"] = 999
    elif mutation == "wrong_hash":
        values["output_hash"] = "0" * 64
    elif mutation == "wrong_model":
        values["model_name"] = "other-model.onnx"
    elif mutation == "wrong_identity":
        values["input_hash"] = "f" * 64
    else:
        raise AssertionError(f"unknown mutation {mutation}")
    return json.dumps(values, allow_nan=True).encode("utf-8")


def test_encode_request_is_canonical_and_keeps_the_private_feature_on_stdin_only() -> None:
    request = _request()

    encoded = encode_request(request)

    assert encoded == canonical_json(request.model_dump(mode="json")).encode("utf-8")
    assert b"reference-feature" not in encoded
    assert json.loads(encoded)["reference_feature_b64"] == request.reference_feature_b64


@pytest.mark.parametrize(
    "mutation",
    (
        "unknown_field",
        "nan_score",
        "infinite_score",
        "noncontiguous",
        "reordered",
        "overlap",
        "wrong_hash",
        "wrong_model",
        "wrong_identity",
    ),
)
def test_adapter_response_fails_closed_for_contract_mutations(mutation: str) -> None:
    request = _request()

    with pytest.raises(DomainError, match="adapter response is invalid") as caught:
        decode_response(_mutated_response(request, mutation), expected_request=request)

    assert caught.value.code == "VOICE_ADAPTER_RESPONSE_INVALID"
    assert caught.value.message == "adapter response is invalid"


@pytest.mark.parametrize(
    "payload",
    (
        b'{"input_hash":"one","input_hash":"two"}',
        b"\xff",
        b"{" + b" " * MAX_ADAPTER_RESPONSE_BYTES,
    ),
    ids=("duplicate_key", "non_utf8", "oversized"),
)
def test_adapter_response_rejects_duplicate_non_utf8_and_oversized_payloads(
    payload: bytes,
) -> None:
    with pytest.raises(DomainError, match="adapter response is invalid") as caught:
        decode_response(payload, expected_request=_request())

    assert caught.value.code == "VOICE_ADAPTER_RESPONSE_INVALID"


def test_adapter_response_rejects_more_than_the_segment_limit() -> None:
    request = _request()
    values = json.loads(_response_payload(request))
    segment = values["segments"][0]
    values["segments"] = [
        {
            **segment,
            "end_ms": index * 2 + 2,
            "ordinal": index + 1,
            "start_ms": index * 2 + 1,
        }
        for index in range(MAX_ADAPTER_SEGMENTS + 1)
    ]
    values["output_hash"] = sha256_text(
        canonical_json({key: value for key, value in values.items() if key != "output_hash"})
    )

    with pytest.raises(DomainError, match="adapter response is invalid"):
        decode_response(canonical_json(values).encode("utf-8"), expected_request=request)


def test_reference_feature_has_an_exact_limit_and_is_wiped_after_use() -> None:
    request = _request()

    feature = decode_reference_feature(request, max_bytes=len(b"reference-feature"))
    assert isinstance(feature, bytearray)
    assert bytes(feature) == b"reference-feature"
    wipe_reference_feature(feature)
    assert feature == bytearray(len(b"reference-feature"))

    with pytest.raises(DomainError, match="adapter request is invalid") as caught:
        decode_reference_feature(request, max_bytes=len(b"reference-feature") - 1)
    assert caught.value.code == "VOICE_ADAPTER_REQUEST_INVALID"


def test_request_rejects_invalid_reference_feature_without_leaking_contents() -> None:
    values = _request().model_dump(mode="python")
    values["reference_feature_b64"] = "***private-feature***"

    with pytest.raises(DomainError, match="adapter request is invalid") as caught:
        AdapterRequest.with_canonical_hash(**values)

    assert "private-feature" not in caught.value.message


def test_request_and_response_bind_the_exact_model_version_and_absolute_paths() -> None:
    values = _request().model_dump(mode="python")
    values["model_version"] = "model-v1"
    request = AdapterRequest.with_canonical_hash(**values)
    response = json.loads(_response_payload(_request()))
    response["model_version"] = "model-v1"
    response["output_hash"] = sha256_text(
        canonical_json({key: value for key, value in response.items() if key != "output_hash"})
    )

    assert request.model_version == "model-v1"
    assert decode_response(canonical_json(response).encode("utf-8"), expected_request=request)

    response["model_version"] = "other-v1"
    response["output_hash"] = sha256_text(
        canonical_json({key: value for key, value in response.items() if key != "output_hash"})
    )
    with pytest.raises(DomainError, match="adapter response is invalid"):
        decode_response(canonical_json(response).encode("utf-8"), expected_request=request)

    for field, path in (
        ("audio_path", "relative.wav"),
        ("model_path", "C:/private/models/../model.onnx"),
        ("vad_model_path", "vad.onnx"),
    ):
        values[field] = path
        with pytest.raises(DomainError, match="adapter request is invalid"):
            AdapterRequest.with_canonical_hash(**values)
        values[field] = _request().model_dump(mode="python")[field]


def test_reference_feature_rejects_oversized_encoded_input_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _request().model_dump(mode="python")
    values["reference_feature_b64"] = base64.b64encode(b"x" * 16).decode("ascii")
    values["reference_feature_length"] = 1
    decoded = False

    def _unexpected_decode(*args: object, **kwargs: object) -> bytes:
        nonlocal decoded
        decoded = True
        raise AssertionError("encoded input was decoded")

    monkeypatch.setattr(protocol.base64, "b64decode", _unexpected_decode)
    with pytest.raises(DomainError, match="adapter request is invalid"):
        AdapterRequest.with_canonical_hash(**values)
    assert not decoded


def test_internal_reference_feature_buffers_are_wiped_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _request().model_dump(mode="python")
    wiped: list[bytes] = []
    original_wipe = protocol.wipe_reference_feature

    def _spy_wipe(value: bytearray) -> None:
        wiped.append(bytes(value))
        original_wipe(value)

    monkeypatch.setattr(protocol, "wipe_reference_feature", _spy_wipe)
    request = AdapterRequest.with_canonical_hash(**values)
    encode_request(request)
    assert wiped == [b"reference-feature", b"reference-feature"]

    broken = request.model_copy(update={"reference_feature_sha256": "0" * 64})
    with pytest.raises(DomainError, match="adapter request is invalid"):
        encode_request(broken)
    assert wiped[-1] == b"reference-feature"


def test_response_segment_count_guard_is_not_hidden_by_the_byte_limit() -> None:
    segment = AdapterSegment(
        end_ms=1,
        evidence_hash="d" * 64,
        ordinal=1,
        raw_score=0.5,
        start_ms=0,
    )
    values = {
        "adapter_contract_version": "voice-adapter-v1",
        "input_hash": "a" * 64,
        "model_name": "model.onnx",
        "model_version": "model-v1",
        "output_hash": "b" * 64,
        "proposal": VoiceProposal.LIKELY_PRESENT,
        "segments": tuple(segment for _ in range(MAX_ADAPTER_SEGMENTS + 1)),
        "vad_contract_version": "vad-v1",
    }

    compact_response = AdapterResponse.model_construct(**values)
    with pytest.raises(ValueError, match="invalid adapter response"):
        compact_response._validate_shape()
