import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.voice import media
from market_voice_forecast_ledger.voice.media import (
    ACQUIRE_TIMEOUT_SECONDS,
    NORMALIZE_TIMEOUT_SECONDS,
    MediaAcquirer,
    MediaNormalizer,
    canonical_watch_url,
)
from market_voice_forecast_ledger.voice.process import (
    ADAPTER_TIMEOUT_SECONDS,
    VoiceAdapterProcess,
)
from market_voice_forecast_ledger.voice import process as voice_process
from market_voice_forecast_ledger.voice.protocol import (
    MAX_ADAPTER_RESPONSE_BYTES,
    encode_request,
)
from tests.backend.voice_fakes import (
    FakeAdapterRunner,
    FakeMediaRunner,
    adapter_response_payload,
    fake_runtime_attestation,
    valid_adapter_request,
)


class _WritePipe:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.written += payload

    def close(self) -> None:
        self.closed = True


class _ReadPipe:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.limits: list[int] = []
        self.closed = False

    def read(self, limit: int) -> bytes:
        self.limits.append(limit)
        return self.payload[:limit]

    def close(self) -> None:
        self.closed = True


class _FakePopen:
    def __init__(self, payload: bytes, *, linger: bool = False) -> None:
        self.stdin = _WritePipe()
        self.stdout = _ReadPipe(payload)
        self.linger = linger
        self.killed = False
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.waits.append(timeout)
        if self.linger and not self.killed:
            raise subprocess.TimeoutExpired(["private-python"], timeout)
        return -9 if self.killed else 0

    def kill(self) -> None:
        self.killed = True


def test_downloader_uses_fixed_attested_argv_hidden_window_and_discarded_output(
    tmp_path: Path,
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    job_dir = work_root / "job-1"
    job_dir.mkdir()
    runner = FakeMediaRunner()

    result = MediaAcquirer(runner, attestation, work_root).acquire(
        "abcdefghijk", job_dir
    )

    assert runner.calls == [
        (
            str(attestation.yt_dlp_path),
            "--no-playlist",
            "--no-write-info-json",
            "--no-write-thumbnail",
            "--no-write-subs",
            "--no-write-auto-subs",
            "-f",
            "bestaudio",
            "--js-runtimes",
            f"deno:{attestation.deno_path}",
            "-o",
            str(job_dir / "source.media"),
            "https://www.youtube.com/watch?v=abcdefghijk",
        )
    ]
    assert runner.kwargs == [
        {
            "check": False,
            "creationflags": subprocess.CREATE_NO_WINDOW,
            "shell": False,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "timeout": ACQUIRE_TIMEOUT_SECONDS,
        }
    ]
    assert result.path == (job_dir / "source.media").resolve()
    assert result.sha256 == hashlib.sha256(b"synthetic-media").hexdigest()
    assert tuple(job_dir.iterdir()) == (result.path,)


def test_normalizer_uses_fixed_ffmpeg_argv_and_exact_inventory(tmp_path: Path) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    job_dir = work_root / "job-1"
    job_dir.mkdir()
    source = job_dir / "source.media"
    source.write_bytes(b"source")
    target = job_dir / "normalized.wav"
    runner = FakeMediaRunner(output=b"normalized")

    result = MediaNormalizer(runner, attestation, work_root).normalize(source, target)

    assert runner.calls == [
        (
            str(attestation.ffmpeg_path),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source.resolve()),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(target),
        )
    ]
    assert runner.kwargs[0]["timeout"] == NORMALIZE_TIMEOUT_SECONDS
    assert runner.kwargs[0]["shell"] is False
    assert runner.kwargs[0]["stdout"] is subprocess.DEVNULL
    assert runner.kwargs[0]["stderr"] is subprocess.DEVNULL
    assert result.path == target.resolve()
    assert result.sha256 == hashlib.sha256(b"normalized").hexdigest()
    assert set(job_dir.iterdir()) == {source, target}


@pytest.mark.parametrize(
    "video_id",
    (
        "short",
        "abcdefghijkl",
        "abcdefghi?k",
        "abcdefghij/",
        "abcdefghij%",
        "abcdefghij&",
        "abcdefghij ",
    ),
)
def test_watch_url_rejects_noncanonical_video_ids(video_id: str) -> None:
    with pytest.raises(DomainError, match="media acquisition failed") as caught:
        canonical_watch_url(video_id)
    assert caught.value.code == "VOICE_MEDIA_ACQUISITION_FAILED"


@pytest.mark.parametrize(
    ("runner", "expected_calls"),
    (
        (FakeMediaRunner(returncode=2), 1),
        (FakeMediaRunner(output=None), 1),
        (FakeMediaRunner(output=b""), 1),
        (FakeMediaRunner(extra_name="private-sentinel.txt"), 1),
        (
            FakeMediaRunner(
                error=subprocess.TimeoutExpired(
                    ["private-sentinel"], timeout=1, output=b"private-sentinel"
                )
            ),
            1,
        ),
    ),
    ids=("nonzero", "missing", "empty", "extra", "timeout"),
)
def test_acquisition_failures_are_constant_and_do_not_leak(
    tmp_path: Path, runner: FakeMediaRunner, expected_calls: int
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    job_dir = work_root / "job-1"
    job_dir.mkdir()

    with pytest.raises(DomainError, match="media acquisition failed") as caught:
        MediaAcquirer(runner, attestation, work_root).acquire("abcdefghijk", job_dir)

    assert caught.value.code == "VOICE_MEDIA_ACQUISITION_FAILED"
    assert "private-sentinel" not in str(caught.value)
    assert len(runner.calls) == expected_calls


def test_acquirer_rejects_escape_reparse_and_mutated_executable_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    runner = FakeMediaRunner()
    outside = tmp_path / "outside"
    outside.mkdir()
    acquirer = MediaAcquirer(runner, attestation, work_root)

    with pytest.raises(DomainError, match="media acquisition failed"):
        acquirer.acquire("abcdefghijk", outside)
    assert runner.calls == []

    job_dir = work_root / "job-1"
    job_dir.mkdir()
    monkeypatch.setattr(media, "_is_reparse", lambda path: path == job_dir)
    with pytest.raises(DomainError, match="media acquisition failed"):
        acquirer.acquire("abcdefghijk", job_dir)
    assert runner.calls == []

    monkeypatch.setattr(media, "_is_reparse", lambda path: False)
    attestation.yt_dlp_path.write_bytes(b"mutated-after-attestation")
    with pytest.raises(DomainError, match="media acquisition failed"):
        acquirer.acquire("abcdefghijk", job_dir)
    assert runner.calls == []


def test_acquirer_rechecks_target_reparse_after_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    job_dir = work_root / "job-1"
    job_dir.mkdir()
    changed: set[Path] = set()
    runner = FakeMediaRunner(after_call=lambda path: changed.add(path))
    monkeypatch.setattr(media, "_is_reparse", lambda path: path in changed)

    with pytest.raises(DomainError, match="media acquisition failed"):
        MediaAcquirer(runner, attestation, work_root).acquire("abcdefghijk", job_dir)

    assert len(runner.calls) == 1


def test_acquirer_rejects_forged_attestation_outside_private_data_root(
    tmp_path: Path,
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    outside = tmp_path / "outside-yt-dlp.exe"
    outside.write_bytes(b"synthetic-outside-tool")
    forged = replace(
        attestation,
        yt_dlp_path=outside.resolve(),
        yt_dlp_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
    )
    job_dir = work_root / "job-1"
    job_dir.mkdir()
    runner = FakeMediaRunner()

    with pytest.raises(DomainError, match="media acquisition failed"):
        MediaAcquirer(runner, forged, work_root).acquire("abcdefghijk", job_dir)

    assert runner.calls == []


@pytest.mark.parametrize("mutation", ("escape", "wrong_name", "extra", "reparse"))
def test_normalizer_rejects_unsafe_target_or_inventory(
    tmp_path: Path, mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    job_dir = work_root / "job-1"
    job_dir.mkdir()
    source = job_dir / "source.media"
    source.write_bytes(b"source")
    target = job_dir / "normalized.wav"
    runner = FakeMediaRunner(
        extra_name="unexpected.bin" if mutation == "extra" else None
    )
    if mutation == "escape":
        target = tmp_path / "normalized.wav"
    elif mutation == "wrong_name":
        target = job_dir / "other.wav"
    elif mutation == "reparse":
        monkeypatch.setattr(media, "_is_reparse", lambda path: path == source)

    with pytest.raises(DomainError, match="media normalization failed") as caught:
        MediaNormalizer(runner, attestation, work_root).normalize(source, target)

    assert caught.value.code == "VOICE_MEDIA_NORMALIZATION_FAILED"
    if mutation != "extra":
        assert runner.calls == []


def test_adapter_process_uses_isolated_python_bounded_transport_and_clean_env(
    tmp_path: Path,
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    runner = FakeAdapterRunner()
    source_environment = {
        "SystemRoot": "C:/Windows",
        "TEMP": "C:/Temp",
        "PYTHONNOUSERSITE": "attacker-value",
        "HTTPS_PROXY": "private-proxy",
        "YOUTUBE_API_KEY": "private-api-key",
        "APPDATA": "private-browser-state",
    }

    response = VoiceAdapterProcess(
        runner, attestation, work_root, source_environment=source_environment
    ).score(request)

    assert response.input_hash == request.input_hash
    assert runner.calls == [
        (
            str(attestation.python_path),
            "-I",
            "-m",
            "market_voice_forecast_ledger.voice.adapter_main",
        )
    ]
    assert runner.kwargs == [
        {
            "check": False,
            "creationflags": subprocess.CREATE_NO_WINDOW,
            "env": {
                "SystemRoot": "C:/Windows",
                "TEMP": "C:/Temp",
                "PYTHONNOUSERSITE": "1",
                "PYTHONUTF8": "1",
            },
            "input": encode_request(request),
            "max_stdout_bytes": MAX_ADAPTER_RESPONSE_BYTES,
            "shell": False,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "timeout": ADAPTER_TIMEOUT_SECONDS,
        }
    ]


def test_adapter_empty_source_environment_inherits_nothing(tmp_path: Path) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    runner = FakeAdapterRunner()

    VoiceAdapterProcess(
        runner, attestation, work_root, source_environment={}
    ).score(request)

    assert runner.kwargs[0]["env"] == {
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }


def test_default_adapter_runner_uses_bounded_fake_popen_without_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    fake = _FakePopen(adapter_response_payload(request))
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> _FakePopen:
        popen_calls.append((argv, dict(kwargs)))
        return fake

    monkeypatch.setattr(voice_process.subprocess, "Popen", fake_popen)
    response = VoiceAdapterProcess(
        None, attestation, work_root, source_environment={}
    ).score(request)

    assert response.input_hash == request.input_hash
    assert popen_calls == [
        (
            [
                str(attestation.python_path),
                "-I",
                "-m",
                "market_voice_forecast_ledger.voice.adapter_main",
            ],
            {
                "creationflags": subprocess.CREATE_NO_WINDOW,
                "env": {"PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"},
                "shell": False,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
            },
        )
    ]
    assert fake.stdin.written == encode_request(request)
    assert fake.stdin.closed
    assert fake.stdout.limits == [MAX_ADAPTER_RESPONSE_BYTES + 1]
    assert fake.stdout.closed
    assert not fake.killed


def test_default_adapter_runner_kills_process_that_lingers_after_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    fake = _FakePopen(adapter_response_payload(request), linger=True)
    monkeypatch.setattr(
        voice_process.subprocess,
        "Popen",
        lambda *args, **kwargs: fake,
    )

    with pytest.raises(DomainError, match="voice adapter process failed"):
        VoiceAdapterProcess(
            None, attestation, work_root, source_environment={}
        ).score(request)

    assert fake.killed


@pytest.mark.parametrize(
    "mutation",
    (
        "model_path",
        "model_hash",
        "model_version",
        "vad_path",
        "vad_hash",
        "adapter_version",
        "vad_version",
        "audio_hash",
        "audio_escape",
    ),
)
def test_adapter_request_must_match_attestation_and_private_audio(
    tmp_path: Path, mutation: str
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    values = valid_adapter_request(attestation, work_root).model_dump(mode="python")
    mutations = {
        "model_path": ("model_path", str((tmp_path / "other.onnx").resolve())),
        "model_hash": ("model_sha256", "0" * 64),
        "model_version": ("model_version", "other-v1"),
        "vad_path": ("vad_model_path", str((tmp_path / "other-vad.onnx").resolve())),
        "vad_hash": ("vad_model_sha256", "0" * 64),
        "adapter_version": ("adapter_contract_version", "other-adapter-v1"),
        "vad_version": ("vad_contract_version", "other-vad-v1"),
        "audio_hash": ("audio_sha256", "0" * 64),
        "audio_escape": ("audio_path", str((tmp_path / "outside.wav").resolve())),
    }
    field, value = mutations[mutation]
    values[field] = value
    request = type(valid_adapter_request(attestation, work_root)).with_canonical_hash(
        **values
    )
    runner = FakeAdapterRunner()

    with pytest.raises(DomainError, match="voice adapter process failed") as caught:
        VoiceAdapterProcess(runner, attestation, work_root).score(request)

    assert caught.value.code == "VOICE_ADAPTER_PROCESS_FAILED"
    assert runner.calls == []


@pytest.mark.parametrize("failure", ("timeout", "nonzero", "oversized", "malformed"))
def test_adapter_failures_are_bounded_constant_and_nonleaking(
    tmp_path: Path, failure: str
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    if failure == "timeout":
        runner = FakeAdapterRunner(
            error=subprocess.TimeoutExpired(
                ["private-python"], 1, output=b"private-sentinel"
            )
        )
    elif failure == "nonzero":
        runner = FakeAdapterRunner(returncode=1, stdout=b"private-sentinel")
    elif failure == "oversized":
        runner = FakeAdapterRunner(stdout=b"x" * (MAX_ADAPTER_RESPONSE_BYTES + 1))
    else:
        runner = FakeAdapterRunner(stdout=b'{"private-sentinel":')

    with pytest.raises(DomainError) as caught:
        VoiceAdapterProcess(runner, attestation, work_root).score(request)

    assert caught.value.code in {
        "VOICE_ADAPTER_PROCESS_FAILED",
        "VOICE_ADAPTER_RESPONSE_INVALID",
    }
    assert caught.value.message in {
        "voice adapter process failed",
        "adapter response is invalid",
    }
    assert "private-sentinel" not in str(caught.value)


def test_process_rejects_attested_python_mutation_before_spawn(tmp_path: Path) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    runner = FakeAdapterRunner()
    attestation.python_path.write_bytes(b"mutated-python")

    with pytest.raises(DomainError, match="voice adapter process failed"):
        VoiceAdapterProcess(runner, attestation, work_root).score(request)

    assert runner.calls == []


def test_process_rejects_forged_attestation_outside_private_data_root(
    tmp_path: Path,
) -> None:
    attestation, work_root = fake_runtime_attestation(tmp_path)
    request = valid_adapter_request(attestation, work_root)
    outside = tmp_path / "outside-python.exe"
    outside.write_bytes(b"synthetic-outside-python")
    forged = replace(
        attestation,
        python_path=outside.resolve(),
        python_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
    )
    runner = FakeAdapterRunner()

    with pytest.raises(DomainError, match="voice adapter process failed"):
        VoiceAdapterProcess(runner, forged, work_root).score(request)

    assert runner.calls == []
