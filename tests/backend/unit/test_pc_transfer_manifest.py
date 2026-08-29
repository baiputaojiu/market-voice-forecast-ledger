from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

import pytest

from market_voice_forecast_ledger.domain.common import canonical_json
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.manifest import (
    BundleMember,
    DatabaseSummary,
    RuntimeModel,
    RuntimeSummary,
    TransferManifest,
    compute_bundle_id,
    decode_manifest,
    encode_manifest,
    validate_member_path,
)


@contextmanager
def manifest_error() -> Iterator[None]:
    with pytest.raises(DomainError) as error:
        yield
    assert error.value.code == "PC_TRANSFER_MANIFEST_INVALID"


def _member(path: str, role: str, digit: str = "3") -> BundleMember:
    return BundleMember(
        path=path,
        role=role,
        size_bytes=10,
        sha256=digit * 64,
    )


def sample_manifest() -> TransferManifest:
    members = (
        BundleMember(
            path="data/ledger.sqlite3",
            role="database",
            size_bytes=123,
            sha256="2" * 64,
        ),
        _member(
            "operator-state/presence-verification/progress.md",
            "operator-state",
        ),
        _member("portable/voice-install/deno.exe", "runtime-tool"),
        _member("portable/voice-install/ffmpeg.exe", "runtime-tool"),
        _member(
            "portable/voice-install/"
            "market_voice_forecast_ledger-0.1.0-py3-none-any.whl",
            "project-wheel",
        ),
        _member("portable/voice-install/yt-dlp.exe", "runtime-tool"),
        _member(
            "portable/voice-models/"
            "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
            "model",
        ),
        _member("portable/voice-models/silero_vad.onnx", "model", "4"),
        _member(
            "portable/voice-models/wespeaker_zh_cnceleb_resnet34.onnx",
            "model",
        ),
        _member(
            "portable/voice-wheelhouse/"
            "annotated_types-0.8.0-py3-none-any.whl",
            "runtime-wheel",
        ),
        _member(
            "portable/voice-wheelhouse/pydantic-2.13.4-py3-none-any.whl",
            "runtime-wheel",
        ),
        _member(
            "portable/voice-wheelhouse/"
            "pydantic_core-2.46.4-cp314-cp314-win_amd64.whl",
            "runtime-wheel",
        ),
        _member(
            "portable/voice-wheelhouse/requirements-runtime.txt",
            "runtime-requirements",
        ),
        _member(
            "portable/voice-wheelhouse/"
            "sherpa_onnx-1.13.4-cp314-cp314-win_amd64.whl",
            "runtime-wheel",
        ),
        _member(
            "portable/voice-wheelhouse/"
            "sherpa_onnx_core-1.13.4-py3-none-win_amd64.whl",
            "runtime-wheel",
        ),
        _member(
            "portable/voice-wheelhouse/"
            "typing_extensions-4.16.0-py3-none-any.whl",
            "runtime-wheel",
        ),
        _member(
            "portable/voice-wheelhouse/"
            "typing_inspection-0.4.4-py3-none-any.whl",
            "runtime-wheel",
        ),
    )
    manifest = TransferManifest(
        schema="market-voice-pc-transfer.v1",
        bundle_id="0" * 64,
        created_at_utc="2026-08-29T03:04:05.000000Z",
        repository_url=(
            "https://github.com/example/market-voice-forecast-ledger.git"
        ),
        branch="feature/presence-verification",
        commit_sha="1" * 40,
        source_tree_clean=True,
        remote_verified=True,
        schedule_local_time="06:00",
        runtime_rebuild_required=True,
        credential_registration_required=True,
        schedule_install_required=True,
        operator_state_destination=(
            ".superpowers/sdd/2026-08-22-presence-verification"
        ),
        database=DatabaseSummary(
            integrity_check="ok",
            migrations=(
                "0001_foundation.sql",
                "0020_presence_verification.sql",
            ),
            table_counts=(
                ("jobs", 20),
                ("voice_reference_features", 4),
            ),
            reference_feature_count=4,
            active_artifact_count=0,
            snapshot_sha256="2" * 64,
        ),
        runtime=RuntimeSummary(
            python_version="3.14.6",
            sherpa_onnx_version="1.13.4",
            yt_dlp_version="2026.08.19",
            deno_version="2.9.5",
            ffmpeg_version="9.0.1",
            vad_version="silero-vad-v5",
            provider="CPUExecutionProvider",
            adapter_contract_version="voice-adapter-v1",
            vad_contract_version="vad-v1",
            models=(
                RuntimeModel(
                    lock_name="runtime-lock.campplus.json",
                    model_name="3dspeaker",
                    model_version="campplus",
                    model_member=(
                        "portable/voice-models/"
                        "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
                    ),
                    vad_member="portable/voice-models/silero_vad.onnx",
                    active=True,
                ),
                RuntimeModel(
                    lock_name="runtime-lock.wespeaker.json",
                    model_name="wespeaker",
                    model_version="zh-cnceleb-resnet34",
                    model_member=(
                        "portable/voice-models/"
                        "wespeaker_zh_cnceleb_resnet34.onnx"
                    ),
                    vad_member="portable/voice-models/silero_vad.onnx",
                    active=False,
                ),
            ),
        ),
        members=members,
    )
    return replace(manifest, bundle_id=compute_bundle_id(manifest))


def test_manifest_round_trip_is_canonical() -> None:
    manifest = sample_manifest()
    raw = encode_manifest(manifest)
    assert raw.endswith(b"\n")
    assert decode_manifest(raw) == manifest
    assert encode_manifest(decode_manifest(raw)) == raw


@pytest.mark.parametrize(
    "path",
    (
        "/data/ledger.sqlite3",
        "C:/data/ledger.sqlite3",
        "../ledger.sqlite3",
        "data\\ledger.sqlite3",
        "data//ledger.sqlite3",
        "data/./ledger.sqlite3",
        "data/ledger.sqlite3:stream",
        "data/NUL.txt",
        "data/trailing.",
        "data/trailing ",
        "data/control\x00name",
    ),
)
def test_member_path_rejects_non_portable_forms(path: str) -> None:
    with manifest_error():
        validate_member_path(path)


def test_manifest_rejects_unknown_top_level_key() -> None:
    raw = json.loads(encode_manifest(sample_manifest()))
    raw["source_path"] = "C:/Users/example"
    encoded = (canonical_json(raw) + "\n").encode()
    with manifest_error():
        decode_manifest(encoded)


def test_manifest_rejects_changed_identity() -> None:
    manifest = sample_manifest()
    changed = replace(manifest, branch="feature/other")
    with manifest_error():
        encode_manifest(changed)


def test_manifest_rejects_case_fold_collision() -> None:
    manifest = sample_manifest()
    collision = BundleMember(
        path="DATA/ledger.sqlite3",
        role="database",
        size_bytes=123,
        sha256="2" * 64,
    )
    candidate = replace(manifest, members=manifest.members + (collision,))
    with manifest_error():
        compute_bundle_id(candidate)


def test_manifest_rejects_two_active_models() -> None:
    manifest = sample_manifest()
    second = replace(manifest.runtime.models[1], active=True)
    runtime = replace(
        manifest.runtime,
        models=(manifest.runtime.models[0], second),
    )
    candidate = replace(manifest, runtime=runtime)
    with manifest_error():
        compute_bundle_id(candidate)


def test_manifest_rejects_remote_url_with_embedded_credential() -> None:
    manifest = sample_manifest()
    candidate = replace(
        manifest,
        repository_url=(
            "https://user:example-token@example.invalid/repository.git"
        ),
    )
    with manifest_error():
        compute_bundle_id(candidate)


def test_manifest_rejects_missing_fixed_member() -> None:
    manifest = sample_manifest()
    candidate = replace(
        manifest,
        members=tuple(
            member
            for member in manifest.members
            if member.path != "portable/voice-install/deno.exe"
        ),
    )
    with manifest_error():
        compute_bundle_id(candidate)


def test_manifest_rejects_duplicate_json_key() -> None:
    raw = encode_manifest(sample_manifest()).decode("utf-8")
    duplicate = raw.replace(
        '{"branch":',
        '{"branch":"feature/duplicate","branch":',
        1,
    ).encode("utf-8")
    with manifest_error():
        decode_manifest(duplicate)
