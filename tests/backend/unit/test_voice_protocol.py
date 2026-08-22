import base64
import ast
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

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
from market_voice_forecast_ledger.voice import adapter_main
from tests.backend.voice_fakes import (
    adapter_response_payload,
    fake_runtime_attestation,
    valid_adapter_request,
)


def _request() -> AdapterRequest:
    feature = b"reference-feature"
    values = {
        "adapter_contract_version": "voice-adapter-v1",
        "audio_duration_ms": 2_000,
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
            },
            {
                "end_ms": 2000,
                "evidence_hash": sha256_text(
                    canonical_json(
                        {
                            "audio_sha256": request.audio_sha256,
                            "end_ms": 2000,
                            "ordinal": 2,
                            "raw_score": 0.85,
                            "start_ms": 1000,
                        }
                    )
                ),
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
    elif mutation == "empty_segments":
        values["segments"] = []
    elif mutation == "evidence_drift":
        values["segments"][0]["evidence_hash"] = "0" * 64
    elif mutation == "inconsistent_proposal":
        values["proposal"] = VoiceProposal.LIKELY_ABSENT.value
    elif mutation == "out_of_audio":
        values["segments"][1]["end_ms"] = 2_001
        segment = values["segments"][1]
        segment["evidence_hash"] = sha256_text(
            canonical_json(
                {
                    "audio_sha256": request.audio_sha256,
                    "end_ms": 2_001,
                    "ordinal": 2,
                    "raw_score": 0.85,
                    "start_ms": 1_000,
                }
            )
        )
    else:
        raise AssertionError(f"unknown mutation {mutation}")
    if mutation in {
        "empty_segments",
        "evidence_drift",
        "inconsistent_proposal",
        "out_of_audio",
    }:
        values["output_hash"] = sha256_text(
            canonical_json(
                {
                    key: value
                    for key, value in values.items()
                    if key != "output_hash"
                }
            )
        )
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
        "empty_segments",
        "evidence_drift",
        "inconsistent_proposal",
        "out_of_audio",
    ),
)
def test_adapter_response_fails_closed_for_contract_mutations(mutation: str) -> None:
    request = _request()

    with pytest.raises(DomainError, match="adapter response is invalid") as caught:
        decode_response(_mutated_response(request, mutation), expected_request=request)

    assert caught.value.code == "VOICE_ADAPTER_RESPONSE_INVALID"
    assert caught.value.message == "adapter response is invalid"


def test_request_binds_audio_duration_into_its_canonical_input_hash() -> None:
    values = _request().model_dump(mode="python")
    values["audio_duration_ms"] = 2_000

    request = AdapterRequest.with_canonical_hash(**values)

    assert request.audio_duration_ms == 2_000
    changed_duration = dict(values)
    changed_duration["audio_duration_ms"] = 2_001
    assert request.input_hash != AdapterRequest.with_canonical_hash(
        **changed_duration
    ).input_hash
    without_duration = dict(values)
    without_duration.pop("audio_duration_ms")
    with pytest.raises(DomainError, match="adapter request is invalid"):
        AdapterRequest.with_canonical_hash(**without_duration)


@pytest.mark.parametrize(
    ("raw_score", "proposal"),
    (
        (0.1, VoiceProposal.LIKELY_ABSENT),
        (0.5, VoiceProposal.NEEDS_REVIEW),
        (0.8, VoiceProposal.LIKELY_PRESENT),
    ),
)
def test_adapter_response_accepts_each_canonical_threshold_classification(
    raw_score: float, proposal: VoiceProposal
) -> None:
    request = _request()
    segment = {
        "end_ms": 1_000,
        "evidence_hash": sha256_text(
            canonical_json(
                {
                    "audio_sha256": request.audio_sha256,
                    "end_ms": 1_000,
                    "ordinal": 1,
                    "raw_score": raw_score,
                    "start_ms": 0,
                }
            )
        ),
        "ordinal": 1,
        "raw_score": raw_score,
        "start_ms": 0,
    }
    values: dict[str, object] = {
        "adapter_contract_version": request.adapter_contract_version,
        "input_hash": request.input_hash,
        "model_name": request.model_name,
        "model_version": request.model_version,
        "proposal": proposal.value,
        "segments": [segment],
        "vad_contract_version": request.vad_contract_version,
    }
    values["output_hash"] = sha256_text(canonical_json(values))

    response = decode_response(
        canonical_json(values).encode("utf-8"), expected_request=request
    )

    assert response.proposal is proposal


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


def test_adapter_entrypoint_denies_socket_before_backend_and_wipes_feature(
    tmp_path: Path,
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    captured: dict[str, object] = {}

    class FakeBackend:
        def score(self) -> AdapterResponse:
            feature = captured["feature"]
            assert isinstance(feature, bytearray)
            assert feature == bytearray(len(feature))
            with pytest.raises(OSError, match="network disabled"):
                socket_module.socket()
            return decode_response(
                adapter_response_payload(request), expected_request=request
            )

    def backend_factory(
        received: AdapterRequest, feature: bytearray
    ) -> FakeBackend:
        assert received == request
        for factory in (
            socket_module.socket,
            socket_module.SocketType,
            socket_module.create_connection,
            socket_module.getaddrinfo,
            socket_module.socketpair,
            low_level_socket_module.socket,
            low_level_socket_module.socketpair,
        ):
            with pytest.raises(OSError, match="network disabled"):
                factory()
        captured["feature"] = feature
        return FakeBackend()

    socket_module = SimpleNamespace(
        socket=lambda: "unsafe",
        SocketType=lambda: "unsafe",
        create_connection=lambda: "unsafe",
        getaddrinfo=lambda: "unsafe",
        socketpair=lambda: "unsafe",
    )
    low_level_socket_module = SimpleNamespace(
        socket=lambda: "unsafe", socketpair=lambda: "unsafe"
    )
    output = adapter_main.process_payload(
        encode_request(request),
        backend_factory=backend_factory,
        socket_module=socket_module,
        low_level_socket_module=low_level_socket_module,
    )

    assert (
        decode_response(output, expected_request=request).input_hash
        == request.input_hash
    )
    assert output.count(b"{") >= 1
    assert json.loads(output)["output_hash"]


def test_adapter_entrypoint_wipes_feature_when_initialization_fails(
    tmp_path: Path,
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    captured: list[bytearray] = []

    def failing_factory(
        received: AdapterRequest, feature: bytearray
    ) -> object:
        captured.append(feature)
        raise RuntimeError(f"private-sentinel {received.audio_path}")

    with pytest.raises(DomainError, match="voice adapter process failed") as caught:
        adapter_main.process_payload(
            encode_request(request),
            backend_factory=failing_factory,
            socket_module=SimpleNamespace(
                socket=lambda: None,
                create_connection=lambda: None,
                getaddrinfo=lambda: None,
            ),
            low_level_socket_module=SimpleNamespace(
                socket=lambda: None,
                socketpair=lambda: None,
            ),
        )

    assert caught.value.code == "VOICE_ADAPTER_PROCESS_FAILED"
    assert captured[0] == bytearray(len(captured[0]))
    assert "private-sentinel" not in str(caught.value)
    assert request.audio_path not in str(caught.value)


def _adapter_forbidden_imports(source: str) -> tuple[str, ...]:
    forbidden = {
        "analysis",
        "api",
        "credentials",
        "db",
        "repositories",
        "services",
        "socket",
        "sqlite3",
        "subprocess",
        "sherpa_onnx",
        "youtube",
    }
    found: set[str] = set()
    tree = ast.parse(source)
    importlib_modules = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "importlib"
    }
    import_functions = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "importlib"
        for alias in node.names
        if alias.name == "import_module"
    }
    for node in ast.walk(tree):
        names: tuple[tuple[str, str], ...] = ()
        if isinstance(node, ast.Import):
            names = tuple((alias.name, "direct") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = tuple(
                (
                    f"{module}.{alias.name}" if module else alias.name,
                    "direct",
                )
                for alias in node.names
            )
        elif (
            isinstance(node, ast.Call)
            and len(node.args) >= 1
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str
        ):
            is_importlib = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_modules
            ) or (
                isinstance(node.func, ast.Name)
                and node.func.id in import_functions
            )
            is_builtin = (
                isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
            )
            if is_importlib or is_builtin:
                names = (
                    (
                        node.args[0].value,
                        "importlib" if is_importlib else "builtin",
                    ),
                )
        for name, kind in names:
            if name in {"socket", "_socket"} and kind == "direct":
                continue
            if name == "sherpa_onnx" and kind == "importlib":
                continue
            parts = set(name.lower().replace("-", "_").split("."))
            found.update(parts & forbidden)
    return tuple(sorted(found))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("import subprocess", ("subprocess",)),
        ("from market_voice_forecast_ledger.db import database", ("db",)),
        (
            "def load():\n"
            "    from market_voice_forecast_ledger.services import youtube",
            ("services", "youtube"),
        ),
        ("from . import db as storage", ("db",)),
        (
            "from market_voice_forecast_ledger.credentials.windows import Vault as V",
            ("credentials",),
        ),
        (
            "from market_voice_forecast_ledger.api import app\nimport analysis as a",
            ("analysis", "api"),
        ),
        (
            "import importlib\n"
            "repository = importlib.import_module(\n"
            "    'market_voice_forecast_ledger.repositories.voice'\n"
            ")",
            ("repositories",),
        ),
        ("from sqlite3 import connect as open_database", ("sqlite3",)),
        (
            "import importlib as loader\n"
            "network = loader.import_module('socket')",
            ("socket",),
        ),
        ("import sherpa_onnx", ("sherpa_onnx",)),
        ("__import__('sherpa_onnx')", ("sherpa_onnx",)),
    ),
)
def test_adapter_import_guard_detects_forbidden_mutations(
    mutation: str, expected: tuple[str, ...]
) -> None:
    assert _adapter_forbidden_imports(mutation) == expected


def test_adapter_entrypoint_has_no_forbidden_imports() -> None:
    source = Path(adapter_main.__file__).read_text(encoding="utf-8")
    assert _adapter_forbidden_imports(source) == ()
