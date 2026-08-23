from __future__ import annotations

from dataclasses import dataclass

import pytest

import market_voice_forecast_ledger.cli as cli
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import ReviewAction


PRIVATE_SENTINELS = (
    "C:/private/local/audio.wav",
    "PRIVATE_EMBEDDING_BYTES",
    "provider-header-secret",
    "native stderr body",
)


@dataclass(frozen=True)
class FakeClip:
    subject_id: int
    video_id: int
    start_ms: int
    end_ms: int
    ordinal: int
    clip_kind: str
    reason: str = PRIVATE_SENTINELS[0]


@dataclass(frozen=True)
class FakePreview:
    preview_hash: str


@dataclass(frozen=True)
class FakeCreation:
    job_ids: tuple[int, ...]


@dataclass(frozen=True)
class FakeSegment:
    start_ms: int
    end_ms: int
    score: float


@dataclass(frozen=True)
class FakeReviewDetail:
    run_id: int
    person_display_name: str
    watch_url: str
    youtube_video_id: str
    segments: tuple[FakeSegment, ...]
    proposal: str
    model_name: str
    model_version: str
    adapter_version: str
    threshold_version: str
    private_path: str = PRIVATE_SENTINELS[0]


class FakeReferenceService:
    def __init__(self) -> None:
        self.approved = None
        self.list_calls = 0

    def list_all_candidates(self):
        self.list_calls += 1
        return (FakeClip(1, 22, 0, 8_000, 1, "enrollment"),)

    def approve_clip(self, command):
        self.approved = command
        return FakeClip(
            command.subject_id,
            command.video_id,
            command.start_ms,
            command.end_ms,
            1,
            "enrollment",
        )


class FakePresenceService:
    def __init__(self) -> None:
        self.preview = FakePreview("a" * 64)
        self.created_with: list[str] = []
        self.reviewed = None
        self.detail = FakeReviewDetail(
            7,
            "Public Person",
            "https://www.youtube.com/watch?v=abcdefghijk",
            "abcdefghijk",
            (FakeSegment(0, 1_000, 0.87654),),
            "likely_present",
            "model.onnx",
            "model-v1",
            "adapter-v1",
            "threshold-v1",
        )

    def preview_pilot(self):
        return self.preview

    def create_pilot(self, expected_preview_hash: str):
        self.created_with.append(expected_preview_hash)
        return FakeCreation(tuple(range(1, 21)))

    def list_pending_reviews(self):
        return (self.detail,)

    def show_review(self, run_id: int):
        assert run_id == 7
        return self.detail

    def review(self, command):
        self.reviewed = command
        return object()


def _fakes():
    reference = FakeReferenceService()
    presence = FakePresenceService()
    return reference, presence


def _run(argv, reference, presence, **extra):
    dependencies = {
        "reference_service_factory": lambda: reference,
        "presence_service_factory": lambda: presence,
        "calibration_runner": lambda: None,
        "presence_worker_runner": lambda: None,
    }
    dependencies.update(extra)
    return cli.run_cli(argv, **dependencies)


def test_presence_commands_use_selected_injected_dependencies_and_safe_output(
    capsys,
) -> None:
    reference, presence = _fakes()

    assert _run(["presence", "reference", "list-candidates"], reference, presence) == 0
    output = capsys.readouterr().out
    assert "Presence reference candidate: subject 1, video 22, enrollment 1, 0-8000 ms." in output
    assert all(value not in output for value in PRIVATE_SENTINELS)

    assert _run(
        [
            "presence", "reference", "approve", "--subject-id", "1",
            "--video-id", "22", "--start-ms", "3000", "--end-ms", "8000",
        ],
        reference,
        presence,
    ) == 0
    assert reference.approved.reason == "approved_reference_clip"
    assert capsys.readouterr().out == "Presence reference approved: subject 1, video 22.\n"

    assert _run(["presence", "calibrate"], reference, presence) == 0
    assert capsys.readouterr().out == "Presence calibration completed.\n"

    assert _run(["presence", "pilot", "create"], reference, presence) == 0
    assert presence.created_with == [presence.preview.preview_hash]
    assert capsys.readouterr().out == "Presence pilot created: 20 jobs.\n"

    assert _run(["presence", "worker", "--once"], reference, presence) == 0
    assert capsys.readouterr().out == "Presence worker completed.\n"

    assert _run(["presence", "review", "list"], reference, presence) == 0
    output = capsys.readouterr().out
    assert "Presence review: run 7, Public Person," in output
    assert all(value not in output for value in PRIVATE_SENTINELS)
    assert _run(["presence", "review", "show", "7"], reference, presence) == 0
    output = capsys.readouterr().out
    assert "https://www.youtube.com/watch?v=abcdefghijk" in output
    assert "0.8765" in output
    assert all(value not in output for value in PRIVATE_SENTINELS)


@pytest.mark.parametrize("action", ("confirm", "reject", "hold"))
def test_presence_review_actions_are_exact_and_human_readable(action, capsys) -> None:
    reference, presence = _fakes()

    assert _run(
        ["presence", "review", action, "7", "--reason", "reviewed segment"],
        reference,
        presence,
    ) == 0

    assert presence.reviewed.action is ReviewAction(action)
    assert presence.reviewed.reason == "reviewed segment"
    assert capsys.readouterr().out == f"Presence review recorded: {action}.\n"


@pytest.mark.parametrize(
    "argv",
    (
        ["presence", "pilot", "create", "--unknown"],
        ["presence", "wor", "--once"],
        ["presence", "worker", "--onc"],
        ["presence", "reference", "approve", "--subject-id", "1"],
        ["presence", "reference", "approve", "--subject-id", "one", "--video-id", "2", "--start-ms", "3", "--end-ms", "4"],
        ["presence", "reference", "approve", "--subject-id", "0", "--video-id", "2", "--start-ms", "3", "--end-ms", "4"],
        ["presence", "reference", "approve", "--subject-id", "1", "--video-id", "2", "--start-ms", "5", "--end-ms", "4"],
        ["presence", "review", "show", "0"],
        ["presence", "review", "confirm", "7", "--reason", "a", "--reason", "b"],
        ["presence", "review", "reject", "7"],
        ["presence", "review", "hold", "7", "--reason", "body=PRIVATE_EMBEDDING_BYTES", "extra"],
        ["presence", "worker", "--once", "--once"],
    ),
)
def test_presence_parser_rejects_invalid_unabbreviated_duplicate_and_body_args(argv):
    reference, presence = _fakes()

    with pytest.raises(SystemExit) as caught:
        cli.main(
            argv,
            reference_service_factory=lambda: reference,
            presence_service_factory=lambda: presence,
            calibration_runner=lambda: None,
            presence_worker_runner=lambda: None,
        )

    assert caught.value.code == 2


def test_presence_commands_never_construct_unselected_dependencies() -> None:
    reference, presence = _fakes()

    def forbidden():
        raise AssertionError("unselected dependency constructed")

    assert cli.main(
        ["presence", "review", "list"],
        reference_service_factory=forbidden,
        presence_service_factory=lambda: presence,
        calibration_runner=forbidden,
        presence_worker_runner=forbidden,
        credential_store_factory=forbidden,
        task_scheduler_factory=forbidden,
        worker_runner=forbidden,
    ) == 0
    calls: list[str] = []

    assert cli.main(
        ["presence", "calibrate"],
        reference_service_factory=forbidden,
        presence_service_factory=forbidden,
        calibration_runner=lambda: calls.append("calibrate"),
        presence_worker_runner=forbidden,
        credential_store_factory=forbidden,
        task_scheduler_factory=forbidden,
        worker_runner=forbidden,
    ) == 0
    assert cli.main(
        ["presence", "worker", "--once"],
        reference_service_factory=forbidden,
        presence_service_factory=forbidden,
        calibration_runner=forbidden,
        presence_worker_runner=lambda: calls.append("worker"),
        credential_store_factory=forbidden,
        task_scheduler_factory=forbidden,
        worker_runner=forbidden,
    ) == 0
    assert calls == ["calibrate", "worker"]
    assert cli.main(
        ["presence", "reference", "list-candidates"],
        reference_service_factory=lambda: reference,
        presence_service_factory=forbidden,
        calibration_runner=forbidden,
        presence_worker_runner=forbidden,
        credential_store_factory=forbidden,
        task_scheduler_factory=forbidden,
        worker_runner=forbidden,
    ) == 0


def test_presence_safe_runner_uses_allowlisted_domain_output_and_hides_unknowns(
    capsys,
) -> None:
    reference, presence = _fakes()

    def known_failure():
        raise DomainError("PRESENCE_PILOT_INSUFFICIENT", PRIVATE_SENTINELS[0])

    assert _run(
        ["presence", "pilot", "create"],
        reference,
        presence,
        presence_service_factory=lambda: type(
            "BrokenPresence", (), {"preview_pilot": staticmethod(known_failure)}
        )(),
    ) == 1
    assert capsys.readouterr().err == "Presence pilot unavailable.\n"

    def unknown_failure():
        raise RuntimeError(PRIVATE_SENTINELS[1])

    assert cli.run_cli(
        ["presence", "worker", "--once"],
        presence_worker_runner=unknown_failure,
    ) == 1
    captured = capsys.readouterr()
    assert captured.err == "Presence command failed.\n"
    assert all(value not in captured.err for value in PRIVATE_SENTINELS)
