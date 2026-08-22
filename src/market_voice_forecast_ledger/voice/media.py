"""Fixed-command acquisition and normalization inside a private work root."""

import hashlib
import os
import re
import stat
import subprocess
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.voice.runtime import RuntimeAttestation


ACQUIRE_TIMEOUT_SECONDS = 1_800
NORMALIZE_TIMEOUT_SECONDS = 600
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

CommandRunner = Callable[..., object]
_WINDOWS_ENV = frozenset(
    {"comspec", "pathext", "systemroot", "temp", "tmp", "windir"}
)


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    modified_ns: int
    size: int


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int


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
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runner = runner
        self._attestation = attestation
        self._private_work_root = private_work_root
        self._source_environment = (
            os.environ if source_environment is None else source_environment
        )

    def acquire(self, video_id: str, target_dir: Path) -> AcquiredMedia:
        work_dir: Path | None = None
        work_identity: _DirectoryIdentity | None = None
        download_path: Path | None = None
        try:
            if not callable(self._runner) or not isinstance(
                self._attestation, RuntimeAttestation
            ):
                raise ValueError("invalid media dependencies")
            watch_url = _canonical_watch_url(video_id)
            work_dir = _prepare_private_work_dir(
                target_dir, self._private_work_root
            )
            work_identity = _directory_identity(work_dir)
            data_root = _private_root(self._private_work_root).parent
            download_path = _new_private_target(
                work_dir / "source.media", work_dir
            )
            _require_inventory(work_dir, ())
            yt_dlp_identity = _require_attested_file(
                self._attestation.yt_dlp_path,
                self._attestation.yt_dlp_sha256,
                data_root,
            )
            deno_identity = _require_attested_file(
                self._attestation.deno_path,
                self._attestation.deno_sha256,
                data_root,
            )
            argv = (
                str(self._attestation.yt_dlp_path),
                "--ignore-config",
                "--no-config-locations",
                "--no-plugin-dirs",
                "--no-cache-dir",
                "--downloader",
                "native",
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
                cwd=str(work_dir),
                env=_allowlisted_media_environment(
                    self._source_environment, yt_dlp=True
                ),
            )
            if type(getattr(completed, "returncode", None)) is not int:
                raise ValueError("media process result is invalid")
            if completed.returncode != 0:
                raise ValueError("media process failed")
            work_dir = _private_work_dir(target_dir, self._private_work_root)
            if _directory_identity(work_dir) != work_identity:
                raise ValueError("work directory identity changed")
            _require_attested_file(
                self._attestation.yt_dlp_path,
                self._attestation.yt_dlp_sha256,
                data_root,
                expected_identity=yt_dlp_identity,
            )
            _require_attested_file(
                self._attestation.deno_path,
                self._attestation.deno_sha256,
                data_root,
                expected_identity=deno_identity,
            )
            output = _private_existing_file(download_path, work_dir)
            _require_inventory(work_dir, (output,))
            digest = _nonempty_file_sha256(output)
            return AcquiredMedia(path=output, sha256=digest, video_id=video_id)
        except Exception:
            _remove_unregistered_target(
                download_path,
                work_dir,
                self._private_work_root,
                work_identity,
            )
            raise _acquisition_failed() from None


class MediaNormalizer:
    def __init__(
        self,
        runner: CommandRunner,
        attestation: RuntimeAttestation,
        private_work_root: Path,
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runner = runner
        self._attestation = attestation
        self._private_work_root = private_work_root
        self._source_environment = (
            os.environ if source_environment is None else source_environment
        )

    def normalize(self, source: Path, target: Path) -> NormalizedAudio:
        work_dir: Path | None = None
        work_identity: _DirectoryIdentity | None = None
        target_path: Path | None = None
        try:
            if not callable(self._runner) or not isinstance(
                self._attestation, RuntimeAttestation
            ):
                raise ValueError("invalid normalizer dependencies")
            if not isinstance(source, Path) or not isinstance(target, Path):
                raise ValueError("invalid normalization paths")
            work_dir = _private_work_dir(source.parent, self._private_work_root)
            work_identity = _directory_identity(work_dir)
            data_root = _private_root(self._private_work_root).parent
            source_path = _private_existing_file(source, work_dir)
            if source_path.name != "source.media":
                raise ValueError("unexpected source name")
            target_path = _new_private_target(target, work_dir)
            if target_path != work_dir / "normalized.wav":
                raise ValueError("unexpected normalized target")
            _require_inventory(work_dir, (source_path,))
            source_sha256 = _nonempty_file_sha256(source_path)
            ffmpeg_identity = _require_attested_file(
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
                cwd=str(work_dir),
                env=_allowlisted_media_environment(
                    self._source_environment, yt_dlp=False
                ),
            )
            if type(getattr(completed, "returncode", None)) is not int:
                raise ValueError("normalizer process result is invalid")
            if completed.returncode != 0:
                raise ValueError("normalizer process failed")
            work_dir = _private_work_dir(source.parent, self._private_work_root)
            if _directory_identity(work_dir) != work_identity:
                raise ValueError("work directory identity changed")
            _require_attested_file(
                self._attestation.ffmpeg_path,
                self._attestation.ffmpeg_sha256,
                data_root,
                expected_identity=ffmpeg_identity,
            )
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
            _remove_unregistered_target(
                target_path,
                work_dir,
                self._private_work_root,
                work_identity,
            )
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


def _prepare_private_work_dir(path: Path, configured_root: Path) -> Path:
    root = _private_root(configured_root)
    if not isinstance(path, Path):
        raise ValueError("invalid work directory")
    raw = path.absolute()
    if raw.parent != root:
        raise ValueError("work directory is not a direct child")
    _require_no_reparse(raw.parent)
    if not raw.exists():
        raw.mkdir(mode=0o700)
    return _private_work_dir(raw, root)


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
    path: Path,
    expected_sha256: str,
    data_root: Path,
    *,
    expected_identity: _FileIdentity | None = None,
) -> _FileIdentity:
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
    identity_before = _file_identity(resolved)
    if expected_identity is not None and identity_before != expected_identity:
        raise ValueError("attested artifact identity changed")
    if _file_sha256(resolved) != expected_sha256:
        raise ValueError("attested artifact changed")
    identity_after = _file_identity(resolved)
    if identity_after != identity_before:
        raise ValueError("attested artifact identity changed")
    return identity_after


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
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        attributes = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        path.is_symlink()
        or bool(isjunction(path))
        or bool(attributes & reparse_flag)
    )


def _file_identity(path: Path) -> _FileIdentity:
    value = os.stat(path, follow_symlinks=False)
    return _FileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        modified_ns=value.st_mtime_ns,
        size=value.st_size,
    )


def _directory_identity(path: Path) -> _DirectoryIdentity:
    value = os.stat(path, follow_symlinks=False)
    return _DirectoryIdentity(device=value.st_dev, inode=value.st_ino)


def _allowlisted_media_environment(
    source: Mapping[str, str], *, yt_dlp: bool
) -> dict[str, str]:
    if not isinstance(source, Mapping):
        raise ValueError("invalid media environment")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in source.items():
        if type(key) is not str or type(value) is not str:
            raise ValueError("invalid media environment entry")
        folded = key.casefold()
        if folded in _WINDOWS_ENV:
            if folded in seen or not value or "\x00" in value:
                raise ValueError("invalid media environment entry")
            result[key] = value
            seen.add(folded)
    if yt_dlp:
        result["YTDLP_NO_PLUGINS"] = "1"
    return result


def _remove_unregistered_target(
    target: Path | None,
    work_dir: Path | None,
    configured_root: Path,
    expected_work_identity: _DirectoryIdentity | None,
) -> None:
    if target is None or work_dir is None or expected_work_identity is None:
        return
    try:
        verified_work = _private_work_dir(work_dir, configured_root)
        if (
            verified_work != work_dir
            or _directory_identity(verified_work) != expected_work_identity
            or target.parent != verified_work
            or target.name not in {"source.media", "normalized.wav"}
        ):
            return
        target_stat = os.lstat(target)
        if stat.S_ISREG(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode):
            os.unlink(target)
    except OSError:
        return
    except ValueError:
        return


def _nonempty_file_sha256(path: Path) -> str:
    if path.stat().st_size <= 0:
        raise ValueError("private artifact is empty")
    return _file_sha256(path)


def _normalized_wav_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 16_000
                or wav.getcomptype() != "NONE"
            ):
                raise ValueError("invalid normalized audio")
            frames = wav.getnframes()
        duration_ms = (frames * 1_000) // 16_000
        if frames < 1 or duration_ms < 1:
            raise ValueError("normalized audio is empty")
        return duration_ms
    except (EOFError, OSError, wave.Error):
        raise ValueError("invalid normalized audio") from None


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
