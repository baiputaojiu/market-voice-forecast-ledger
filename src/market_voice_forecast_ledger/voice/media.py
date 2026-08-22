"""Fixed-command acquisition and normalization inside a private work root."""

import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.voice.runtime import RuntimeAttestation


ACQUIRE_TIMEOUT_SECONDS = 1_800
NORMALIZE_TIMEOUT_SECONDS = 600
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

CommandRunner = Callable[..., object]


@dataclass(frozen=True, slots=True)
class AcquiredMedia:
    path: Path
    sha256: str
    video_id: str


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    path: Path
    sha256: str
    source_sha256: str


class MediaAcquirer:
    def __init__(
        self,
        runner: CommandRunner,
        attestation: RuntimeAttestation,
        private_work_root: Path,
    ) -> None:
        self._runner = runner
        self._attestation = attestation
        self._private_work_root = private_work_root

    def acquire(self, video_id: str, target_dir: Path) -> AcquiredMedia:
        try:
            if not callable(self._runner) or not isinstance(
                self._attestation, RuntimeAttestation
            ):
                raise ValueError("invalid media dependencies")
            watch_url = _canonical_watch_url(video_id)
            work_dir = _private_work_dir(target_dir, self._private_work_root)
            data_root = _private_root(self._private_work_root).parent
            download_path = _new_private_target(
                work_dir / "source.media", work_dir
            )
            _require_inventory(work_dir, ())
            _require_attested_file(
                self._attestation.yt_dlp_path,
                self._attestation.yt_dlp_sha256,
                data_root,
            )
            _require_attested_file(
                self._attestation.deno_path,
                self._attestation.deno_sha256,
                data_root,
            )
            argv = (
                str(self._attestation.yt_dlp_path),
                "--no-playlist",
                "--no-write-info-json",
                "--no-write-thumbnail",
                "--no-write-subs",
                "--no-write-auto-subs",
                "-f",
                "bestaudio",
                "--js-runtimes",
                f"deno:{self._attestation.deno_path}",
                "-o",
                str(download_path),
                watch_url,
            )
            completed = self._runner(
                argv,
                shell=False,
                timeout=ACQUIRE_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if type(getattr(completed, "returncode", None)) is not int:
                raise ValueError("media process result is invalid")
            if completed.returncode != 0:
                raise ValueError("media process failed")
            work_dir = _private_work_dir(target_dir, self._private_work_root)
            output = _private_existing_file(download_path, work_dir)
            _require_inventory(work_dir, (output,))
            digest = _nonempty_file_sha256(output)
            return AcquiredMedia(path=output, sha256=digest, video_id=video_id)
        except Exception:
            raise _acquisition_failed() from None


class MediaNormalizer:
    def __init__(
        self,
        runner: CommandRunner,
        attestation: RuntimeAttestation,
        private_work_root: Path,
    ) -> None:
        self._runner = runner
        self._attestation = attestation
        self._private_work_root = private_work_root

    def normalize(self, source: Path, target: Path) -> NormalizedAudio:
        try:
            if not callable(self._runner) or not isinstance(
                self._attestation, RuntimeAttestation
            ):
                raise ValueError("invalid normalizer dependencies")
            if not isinstance(source, Path) or not isinstance(target, Path):
                raise ValueError("invalid normalization paths")
            work_dir = _private_work_dir(source.parent, self._private_work_root)
            data_root = _private_root(self._private_work_root).parent
            source_path = _private_existing_file(source, work_dir)
            if source_path.name != "source.media":
                raise ValueError("unexpected source name")
            target_path = _new_private_target(target, work_dir)
            if target_path != work_dir / "normalized.wav":
                raise ValueError("unexpected normalized target")
            _require_inventory(work_dir, (source_path,))
            source_sha256 = _nonempty_file_sha256(source_path)
            _require_attested_file(
                self._attestation.ffmpeg_path,
                self._attestation.ffmpeg_sha256,
                data_root,
            )
            argv = (
                str(self._attestation.ffmpeg_path),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(target_path),
            )
            completed = self._runner(
                argv,
                shell=False,
                timeout=NORMALIZE_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if type(getattr(completed, "returncode", None)) is not int:
                raise ValueError("normalizer process result is invalid")
            if completed.returncode != 0:
                raise ValueError("normalizer process failed")
            work_dir = _private_work_dir(source.parent, self._private_work_root)
            source_path = _private_existing_file(source, work_dir)
            output = _private_existing_file(target_path, work_dir)
            _require_inventory(work_dir, (source_path, output))
            if _nonempty_file_sha256(source_path) != source_sha256:
                raise ValueError("normalization source changed")
            return NormalizedAudio(
                path=output,
                sha256=_nonempty_file_sha256(output),
                source_sha256=source_sha256,
            )
        except Exception:
            raise _normalization_failed() from None


def canonical_watch_url(video_id: str) -> str:
    try:
        return _canonical_watch_url(video_id)
    except Exception:
        raise _acquisition_failed() from None


def _canonical_watch_url(video_id: object) -> str:
    if type(video_id) is not str or _VIDEO_ID.fullmatch(video_id) is None:
        raise ValueError("invalid video id")
    return f"https://www.youtube.com/watch?v={video_id}"


def _private_work_dir(path: Path, configured_root: Path) -> Path:
    root = _private_root(configured_root)
    if not isinstance(path, Path):
        raise ValueError("invalid work directory")
    raw = path.absolute()
    _require_no_reparse(raw)
    resolved = raw.resolve(strict=True)
    if raw != resolved or not resolved.is_dir() or resolved == root:
        raise ValueError("invalid work directory")
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("work directory escaped root") from None
    return resolved


def _private_root(path: Path) -> Path:
    if not isinstance(path, Path):
        raise ValueError("invalid private root")
    raw = path.absolute()
    _require_no_reparse(raw)
    resolved = raw.resolve(strict=True)
    if raw != resolved or not resolved.is_dir():
        raise ValueError("invalid private root")
    return resolved


def _new_private_target(path: Path, work_dir: Path) -> Path:
    raw = path.absolute()
    _require_no_reparse(raw)
    resolved = raw.resolve(strict=False)
    if raw != resolved or resolved.exists():
        raise ValueError("private target already exists")
    try:
        relative = resolved.relative_to(work_dir)
    except ValueError:
        raise ValueError("private target escaped work directory") from None
    if len(relative.parts) != 1:
        raise ValueError("private target is not a direct child")
    return resolved


def _private_existing_file(path: Path, root: Path) -> Path:
    raw = path.absolute()
    _require_no_reparse(raw)
    resolved = raw.resolve(strict=True)
    if raw != resolved or not resolved.is_file() or _is_reparse(resolved):
        raise ValueError("private artifact is invalid")
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise ValueError("private artifact escaped root") from None
    if len(relative.parts) != 1:
        raise ValueError("private artifact is not a direct child")
    return resolved


def _require_attested_file(
    path: Path, expected_sha256: str, data_root: Path
) -> None:
    if not isinstance(path, Path) or type(expected_sha256) is not str:
        raise ValueError("invalid attested artifact")
    raw = path.absolute()
    _require_no_reparse(raw)
    resolved = raw.resolve(strict=True)
    if raw != resolved or not resolved.is_file() or _is_reparse(resolved):
        raise ValueError("attested artifact is invalid")
    try:
        resolved.relative_to(data_root)
    except ValueError:
        raise ValueError("attested artifact escaped private data") from None
    if _file_sha256(resolved) != expected_sha256:
        raise ValueError("attested artifact changed")


def _require_inventory(root: Path, expected: tuple[Path, ...]) -> None:
    actual = tuple(root.iterdir())
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise ValueError("unexpected work inventory")
    if any(_is_reparse(path) for path in actual):
        raise ValueError("work inventory contains reparse point")


def _require_no_reparse(path: Path) -> None:
    anchor = Path(path.anchor)
    current = anchor
    for component in path.relative_to(anchor).parts:
        current = current / component
        if _is_reparse(current):
            raise ValueError("private path contains reparse point")


def _is_reparse(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def _nonempty_file_sha256(path: Path) -> str:
    if path.stat().st_size <= 0:
        raise ValueError("private artifact is empty")
    return _file_sha256(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


def _acquisition_failed() -> DomainError:
    return DomainError(
        "VOICE_MEDIA_ACQUISITION_FAILED", "media acquisition failed"
    )


def _normalization_failed() -> DomainError:
    return DomainError(
        "VOICE_MEDIA_NORMALIZATION_FAILED", "media normalization failed"
    )


__all__ = [
    "ACQUIRE_TIMEOUT_SECONDS",
    "NORMALIZE_TIMEOUT_SECONDS",
    "AcquiredMedia",
    "MediaAcquirer",
    "MediaNormalizer",
    "NormalizedAudio",
    "canonical_watch_url",
]
