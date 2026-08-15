import pytest

from market_voice_forecast_ledger.domain.enums import JobKind, JobStage
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    ANALYSIS_INPUT_UNIT_KEY,
    FINAL_PROMOTION_UNIT_KEY,
    JobManifest,
    ManifestUnit,
)


def _analysis_units() -> tuple[ManifestUnit, ...]:
    return (
        ManifestUnit(
            ANALYSIS_INPUT_UNIT_KEY,
            JobStage.ANALYSIS_INPUT_EXTRACTION,
            1,
            "input-contract",
            (),
            "input-contract-v1",
        ),
        ManifestUnit(
            "codex:batch:1",
            JobStage.CODEX_ANALYSIS,
            2,
            None,
            (ANALYSIS_INPUT_UNIT_KEY,),
            "codex-contract-v1",
        ),
        ManifestUnit(
            FINAL_PROMOTION_UNIT_KEY,
            JobStage.HEATMAP_UPDATE,
            3,
            None,
            ("codex:batch:1",),
            "promotion-contract-v1",
        ),
    )


def test_manifest_hash_is_deterministic_for_the_same_ordinal_manifest():
    units = _analysis_units()

    first = JobManifest.build(JobKind.ANALYSIS_SCOPE, units)
    second = JobManifest.build(JobKind.ANALYSIS_SCOPE, tuple(reversed(units)))

    assert first.units == units
    assert second.units == units
    assert first.manifest_hash == second.manifest_hash


@pytest.mark.parametrize(
    "units",
    [
        (),
        (
            ManifestUnit(
                "video:one",
                JobStage.VIDEO_METADATA,
                2,
                "video-input",
                (),
                "metadata-contract-v1",
            ),
        ),
        (
            ManifestUnit(
                "video:one",
                JobStage.VIDEO_METADATA,
                1,
                "video-input",
                (),
                "metadata-contract-v1",
            ),
            ManifestUnit(
                "audio:one",
                JobStage.AUDIO_ACQUISITION,
                3,
                None,
                ("video:one",),
                "audio-contract-v1",
            ),
        ),
    ],
)
def test_manifest_ordinals_must_be_nonempty_and_contiguous_from_one(units):
    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.VIDEO_PIPELINE, units)

    assert error.value.code == "INVALID_MANIFEST_ORDINALS"


def test_manifest_rejects_duplicate_unit_keys():
    units = (
        ManifestUnit(
            "video:one",
            JobStage.VIDEO_METADATA,
            1,
            "video-input",
            (),
            "metadata-contract-v1",
        ),
        ManifestUnit(
            "video:one",
            JobStage.AUDIO_ACQUISITION,
            2,
            None,
            ("video:one",),
            "audio-contract-v1",
        ),
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.VIDEO_PIPELINE, units)

    assert error.value.code == "DUPLICATE_UNIT_KEY"


@pytest.mark.parametrize(
    "dependencies",
    [
        ("audio:one",),
        ("missing:unit",),
        ("video:one", "video:one"),
    ],
)
def test_manifest_dependencies_must_be_unique_earlier_units(dependencies):
    units = (
        ManifestUnit(
            "video:one",
            JobStage.VIDEO_METADATA,
            1,
            "video-input",
            (),
            "metadata-contract-v1",
        ),
        ManifestUnit(
            "audio:one",
            JobStage.AUDIO_ACQUISITION,
            2,
            None,
            dependencies,
            "audio-contract-v1",
        ),
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.VIDEO_PIPELINE, units)

    assert error.value.code == "INVALID_UNIT_DEPENDENCY"


def test_analysis_manifest_requires_exactly_one_final_promotion_unit():
    units = (
        ManifestUnit(
            ANALYSIS_INPUT_UNIT_KEY,
            JobStage.ANALYSIS_INPUT_EXTRACTION,
            1,
            "input-contract",
            (),
            "contract-hash",
        ),
        ManifestUnit(
            "codex:1",
            JobStage.CODEX_ANALYSIS,
            2,
            None,
            (ANALYSIS_INPUT_UNIT_KEY,),
            "contract-hash",
        ),
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.ANALYSIS_SCOPE, units)

    assert error.value.code == "INVALID_ANALYSIS_MANIFEST"


def test_analysis_manifest_requires_input_freeze_as_first_unit():
    units = (
        ManifestUnit(
            FINAL_PROMOTION_UNIT_KEY,
            JobStage.HEATMAP_UPDATE,
            1,
            None,
            (),
            "contract-hash",
        ),
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.ANALYSIS_SCOPE, units)

    assert error.value.code == "INVALID_ANALYSIS_MANIFEST"


def test_analysis_reserved_units_require_their_exact_stages():
    units = list(_analysis_units())
    units[-1] = ManifestUnit(
        FINAL_PROMOTION_UNIT_KEY,
        JobStage.CODEX_ANALYSIS,
        3,
        None,
        ("codex:batch:1",),
        "promotion-contract-v1",
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.ANALYSIS_SCOPE, units)

    assert error.value.code == "INVALID_PROMOTION_STAGE"


def test_video_manifest_rejects_analysis_reserved_units():
    units = (
        ManifestUnit(
            ANALYSIS_INPUT_UNIT_KEY,
            JobStage.VIDEO_METADATA,
            1,
            "video-input",
            (),
            "metadata-contract-v1",
        ),
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.VIDEO_PIPELINE, units)

    assert error.value.code == "INVALID_VIDEO_MANIFEST"


def test_manifest_rejects_a_stage_from_the_other_job_kind():
    units = (
        ManifestUnit(
            "codex:batch:1",
            JobStage.CODEX_ANALYSIS,
            1,
            "video-input",
            (),
            "codex-contract-v1",
        ),
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.VIDEO_PIPELINE, units)

    assert error.value.code == "INVALID_JOB_STAGE"


def test_manifest_rejects_private_path_like_unit_keys():
    units = (
        ManifestUnit(
            r"C:\private\audio.wav",
            JobStage.AUDIO_ACQUISITION,
            1,
            "audio-input",
            (),
            "audio-contract-v1",
        ),
    )

    with pytest.raises(DomainError) as error:
        JobManifest.build(JobKind.VIDEO_PIPELINE, units)

    assert error.value.code == "INVALID_UNIT_KEY"
