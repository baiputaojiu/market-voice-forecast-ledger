"""Strict canonical manifest for one verified PC-transfer bundle."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError


SCHEMA = "market-voice-pc-transfer.v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_SCHEDULE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MIGRATION = re.compile(r"^\d{4}_[A-Za-z0-9_]+\.sql$")
_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_WINDOWS_INVALID = frozenset('<>"|?*')
_WINDOWS_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{value}" for value in range(1, 10)),
        *(f"lpt{value}" for value in range(1, 10)),
    }
)

MEMBER_ROLES = frozenset(
    {
        "database",
        "model",
        "runtime-requirements",
        "runtime-wheel",
        "runtime-tool",
        "project-wheel",
        "operator-state",
    }
)
MEMBER_PREFIX_BY_ROLE = {
    "database": "data/",
    "model": "portable/voice-models/",
    "runtime-requirements": "portable/voice-wheelhouse/",
    "runtime-wheel": "portable/voice-wheelhouse/",
    "runtime-tool": "portable/voice-install/",
    "project-wheel": "portable/voice-install/",
    "operator-state": "operator-state/presence-verification/",
}
EXACT_MODEL_MEMBERS = frozenset(
    {
        "portable/voice-models/"
        "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        "portable/voice-models/silero_vad.onnx",
        "portable/voice-models/wespeaker_zh_cnceleb_resnet34.onnx",
    }
)
EXACT_RUNTIME_TOOL_MEMBERS = frozenset(
    {
        "portable/voice-install/deno.exe",
        "portable/voice-install/ffmpeg.exe",
        "portable/voice-install/yt-dlp.exe",
    }
)
EXACT_RUNTIME_WHEEL_MEMBERS = frozenset(
    {
        "portable/voice-wheelhouse/annotated_types-0.8.0-py3-none-any.whl",
        "portable/voice-wheelhouse/pydantic-2.13.4-py3-none-any.whl",
        "portable/voice-wheelhouse/"
        "pydantic_core-2.46.4-cp314-cp314-win_amd64.whl",
        "portable/voice-wheelhouse/"
        "sherpa_onnx-1.13.4-cp314-cp314-win_amd64.whl",
        "portable/voice-wheelhouse/"
        "sherpa_onnx_core-1.13.4-py3-none-win_amd64.whl",
        "portable/voice-wheelhouse/"
        "typing_extensions-4.16.0-py3-none-any.whl",
        "portable/voice-wheelhouse/"
        "typing_inspection-0.4.4-py3-none-any.whl",
    }
)
EXACT_REQUIREMENTS_MEMBER = (
    "portable/voice-wheelhouse/requirements-runtime.txt"
)
EXACT_PROJECT_WHEEL_MEMBER = (
    "portable/voice-install/"
    "market_voice_forecast_ledger-0.1.0-py3-none-any.whl"
)
OPERATOR_STATE_DESTINATION = (
    ".superpowers/sdd/2026-08-22-presence-verification"
)
REQUIRED_OPERATOR_MEMBER = (
    "operator-state/presence-verification/progress.md"
)
_MODEL_MEMBER_BY_LOCK = {
    "runtime-lock.campplus.json": (
        "portable/voice-models/"
        "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
    ),
    "runtime-lock.wespeaker.json": (
        "portable/voice-models/wespeaker_zh_cnceleb_resnet34.onnx"
    ),
}


@dataclass(frozen=True, slots=True)
class BundleMember:
    path: str
    role: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DatabaseSummary:
    integrity_check: str
    migrations: tuple[str, ...]
    table_counts: tuple[tuple[str, int], ...]
    reference_feature_count: int
    active_artifact_count: int
    snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    lock_name: str
    model_name: str
    model_version: str
    model_member: str
    vad_member: str
    active: bool


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    python_version: str
    sherpa_onnx_version: str
    yt_dlp_version: str
    deno_version: str
    ffmpeg_version: str
    vad_version: str
    provider: str
    adapter_contract_version: str
    vad_contract_version: str
    models: tuple[RuntimeModel, ...]


@dataclass(frozen=True, slots=True)
class TransferManifest:
    schema: str
    bundle_id: str
    created_at_utc: str
    repository_url: str
    branch: str
    commit_sha: str
    source_tree_clean: bool
    remote_verified: bool
    schedule_local_time: str
    runtime_rebuild_required: bool
    credential_registration_required: bool
    schedule_install_required: bool
    operator_state_destination: str
    database: DatabaseSummary
    runtime: RuntimeSummary
    members: tuple[BundleMember, ...]


def validate_member_path(path: str) -> str:
    try:
        _validate_member_path(path)
        return path
    except Exception:
        raise _manifest_error() from None


def compute_bundle_id(manifest: TransferManifest) -> str:
    try:
        _validate_manifest(manifest)
        normalized = replace(manifest, bundle_id="0" * 64)
        return sha256_text(canonical_json(_manifest_to_object(normalized)))
    except Exception:
        raise _manifest_error() from None


def encode_manifest(manifest: TransferManifest) -> bytes:
    try:
        _validate_manifest(manifest)
        if manifest.bundle_id != compute_bundle_id(manifest):
            raise ValueError("bundle identity mismatch")
        return (canonical_json(_manifest_to_object(manifest)) + "\n").encode(
            "utf-8"
        )
    except Exception:
        raise _manifest_error() from None


def decode_manifest(raw: bytes) -> TransferManifest:
    try:
        if (
            type(raw) is not bytes
            or not raw
            or len(raw) > MAX_MANIFEST_BYTES
            or not raw.endswith(b"\n")
        ):
            raise ValueError("invalid manifest bytes")
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
        manifest = _manifest_from_object(parsed)
        if encode_manifest(manifest) != raw:
            raise ValueError("manifest is not canonical")
        return manifest
    except Exception:
        raise _manifest_error() from None


def _validate_manifest(manifest: TransferManifest) -> None:
    if type(manifest) is not TransferManifest:
        raise ValueError("invalid manifest")
    if (
        manifest.schema != SCHEMA
        or not _is_sha256(manifest.bundle_id)
        or not _is_commit_sha(manifest.commit_sha)
        or manifest.source_tree_clean is not True
        or manifest.remote_verified is not True
        or manifest.runtime_rebuild_required is not True
        or manifest.credential_registration_required is not True
        or manifest.schedule_install_required is not True
        or manifest.operator_state_destination != OPERATOR_STATE_DESTINATION
    ):
        raise ValueError("invalid manifest identity")
    _validate_utc(manifest.created_at_utc)
    _validate_repository_url(manifest.repository_url)
    _validate_branch(manifest.branch)
    if (
        type(manifest.schedule_local_time) is not str
        or _SCHEDULE.fullmatch(manifest.schedule_local_time) is None
    ):
        raise ValueError("invalid schedule")
    _validate_database(manifest.database)
    _validate_runtime(manifest.runtime)
    _validate_members(manifest.members, manifest.database, manifest.runtime)


def _validate_database(database: DatabaseSummary) -> None:
    if type(database) is not DatabaseSummary:
        raise ValueError("invalid database")
    if (
        database.integrity_check != "ok"
        or type(database.migrations) is not tuple
        or not database.migrations
        or type(database.table_counts) is not tuple
        or not database.table_counts
        or type(database.reference_feature_count) is not int
        or database.reference_feature_count <= 0
        or type(database.active_artifact_count) is not int
        or database.active_artifact_count != 0
        or not _is_sha256(database.snapshot_sha256)
    ):
        raise ValueError("invalid database summary")
    if database.migrations != tuple(sorted(set(database.migrations))):
        raise ValueError("non-canonical migrations")
    if not all(
        type(name) is str and _MIGRATION.fullmatch(name) is not None
        for name in database.migrations
    ):
        raise ValueError("invalid migration")
    names: list[str] = []
    for item in database.table_counts:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _TABLE.fullmatch(item[0]) is None
            or type(item[1]) is not int
            or item[1] < 0
        ):
            raise ValueError("invalid table count")
        names.append(item[0])
    if tuple(names) != tuple(sorted(set(names))):
        raise ValueError("non-canonical table counts")


def _validate_runtime(runtime: RuntimeSummary) -> None:
    if type(runtime) is not RuntimeSummary:
        raise ValueError("invalid runtime")
    tokens = (
        runtime.python_version,
        runtime.sherpa_onnx_version,
        runtime.yt_dlp_version,
        runtime.deno_version,
        runtime.ffmpeg_version,
        runtime.vad_version,
        runtime.adapter_contract_version,
        runtime.vad_contract_version,
    )
    if (
        not all(type(value) is str and _TOKEN.fullmatch(value) for value in tokens)
        or runtime.provider != "CPUExecutionProvider"
        or type(runtime.models) is not tuple
        or len(runtime.models) != 2
    ):
        raise ValueError("invalid runtime summary")
    if tuple(model.lock_name for model in runtime.models) != tuple(
        sorted(_MODEL_MEMBER_BY_LOCK)
    ):
        raise ValueError("invalid runtime locks")
    active_count = 0
    for model in runtime.models:
        if type(model) is not RuntimeModel:
            raise ValueError("invalid runtime model")
        if (
            model.lock_name not in _MODEL_MEMBER_BY_LOCK
            or model.model_member != _MODEL_MEMBER_BY_LOCK[model.lock_name]
            or model.vad_member
            != "portable/voice-models/silero_vad.onnx"
            or not _is_token(model.model_name)
            or not _is_token(model.model_version)
            or type(model.active) is not bool
        ):
            raise ValueError("invalid runtime model")
        active_count += int(model.active)
    if active_count != 1:
        raise ValueError("invalid active runtime model")


def _validate_members(
    members: tuple[BundleMember, ...],
    database: DatabaseSummary,
    runtime: RuntimeSummary,
) -> None:
    if (
        type(members) is not tuple
        or not members
        or len(members) > MAX_MEMBERS
    ):
        raise ValueError("invalid members")
    paths: list[str] = []
    folded: set[str] = set()
    total = 0
    by_role: dict[str, set[str]] = {role: set() for role in MEMBER_ROLES}
    member_hashes: dict[str, str] = {}
    for member in members:
        if (
            type(member) is not BundleMember
            or member.role not in MEMBER_ROLES
            or type(member.size_bytes) is not int
            or member.size_bytes < 0
            or member.size_bytes > MAX_MEMBER_BYTES
            or not _is_sha256(member.sha256)
        ):
            raise ValueError("invalid member")
        _validate_member_path(member.path)
        if not member.path.startswith(MEMBER_PREFIX_BY_ROLE[member.role]):
            raise ValueError("member role mismatch")
        key = member.path.casefold()
        if member.path in member_hashes or key in folded:
            raise ValueError("duplicate member")
        total += member.size_bytes
        if total > MAX_TOTAL_BYTES:
            raise ValueError("oversized members")
        paths.append(member.path)
        folded.add(key)
        by_role[member.role].add(member.path)
        member_hashes[member.path] = member.sha256
    if tuple(paths) != tuple(sorted(paths, key=str.casefold)):
        raise ValueError("members are not canonical")
    expected_roles = {
        "database": {"data/ledger.sqlite3"},
        "model": set(EXACT_MODEL_MEMBERS),
        "runtime-requirements": {EXACT_REQUIREMENTS_MEMBER},
        "runtime-wheel": set(EXACT_RUNTIME_WHEEL_MEMBERS),
        "runtime-tool": set(EXACT_RUNTIME_TOOL_MEMBERS),
        "project-wheel": {EXACT_PROJECT_WHEEL_MEMBER},
    }
    for role, expected in expected_roles.items():
        if by_role[role] != expected:
            raise ValueError("unexpected fixed member")
    if (
        not by_role["operator-state"]
        or REQUIRED_OPERATOR_MEMBER not in by_role["operator-state"]
    ):
        raise ValueError("operator state is incomplete")
    if member_hashes["data/ledger.sqlite3"] != database.snapshot_sha256:
        raise ValueError("database member mismatch")
    model_paths = {model.model_member for model in runtime.models}
    vad_paths = {model.vad_member for model in runtime.models}
    if not model_paths <= by_role["model"] or not vad_paths <= by_role["model"]:
        raise ValueError("runtime model member mismatch")
    for role, role_paths in by_role.items():
        if role == "operator-state":
            continue
        expected_parent = PurePosixPath(MEMBER_PREFIX_BY_ROLE[role].rstrip("/"))
        if any(PurePosixPath(path).parent != expected_parent for path in role_paths):
            raise ValueError("nested fixed member")


def _validate_member_path(path: object) -> None:
    if (
        type(path) is not str
        or not path
        or len(path.encode("utf-8")) > 512
        or path.startswith("/")
        or "\\" in path
    ):
        raise ValueError("invalid member path")
    parsed = PurePosixPath(path)
    parts = path.split("/")
    if parsed.as_posix() != path or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("non-canonical member path")
    for part in parts:
        folded_base = part.split(".", 1)[0].casefold()
        if (
            ":" in part
            or part.endswith((" ", "."))
            or folded_base in _WINDOWS_RESERVED
            or any(character in _WINDOWS_INVALID for character in part)
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
        ):
            raise ValueError("non-portable member path")


def _validate_utc(value: object) -> None:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        raise ValueError("invalid UTC")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_repository_url(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid repository URL")
    if "://" not in value:
        return
    parsed = urlsplit(value)
    if (
        not parsed.scheme
        or not parsed.netloc
        or parsed.password is not None
        or (
            parsed.scheme.casefold() in {"http", "https"}
            and parsed.username is not None
        )
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError("unsafe repository URL")


def _validate_branch(value: object) -> None:
    if (
        type(value) is not str
        or _BRANCH.fullmatch(value) is None
        or value.endswith(("/", ".", ".lock"))
        or value.startswith(".")
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(part.startswith(".") for part in value.split("/"))
    ):
        raise ValueError("invalid branch")


def _manifest_to_object(manifest: TransferManifest) -> dict[str, object]:
    return {
        "branch": manifest.branch,
        "bundle_id": manifest.bundle_id,
        "commit_sha": manifest.commit_sha,
        "created_at_utc": manifest.created_at_utc,
        "credential_registration_required": (
            manifest.credential_registration_required
        ),
        "database": {
            "active_artifact_count": manifest.database.active_artifact_count,
            "integrity_check": manifest.database.integrity_check,
            "migrations": list(manifest.database.migrations),
            "reference_feature_count": (
                manifest.database.reference_feature_count
            ),
            "snapshot_sha256": manifest.database.snapshot_sha256,
            "table_counts": [
                [name, count] for name, count in manifest.database.table_counts
            ],
        },
        "members": [
            {
                "path": member.path,
                "role": member.role,
                "sha256": member.sha256,
                "size_bytes": member.size_bytes,
            }
            for member in manifest.members
        ],
        "operator_state_destination": manifest.operator_state_destination,
        "remote_verified": manifest.remote_verified,
        "repository_url": manifest.repository_url,
        "runtime": {
            "adapter_contract_version": (
                manifest.runtime.adapter_contract_version
            ),
            "deno_version": manifest.runtime.deno_version,
            "ffmpeg_version": manifest.runtime.ffmpeg_version,
            "models": [
                {
                    "active": model.active,
                    "lock_name": model.lock_name,
                    "model_member": model.model_member,
                    "model_name": model.model_name,
                    "model_version": model.model_version,
                    "vad_member": model.vad_member,
                }
                for model in manifest.runtime.models
            ],
            "provider": manifest.runtime.provider,
            "python_version": manifest.runtime.python_version,
            "sherpa_onnx_version": manifest.runtime.sherpa_onnx_version,
            "vad_contract_version": manifest.runtime.vad_contract_version,
            "vad_version": manifest.runtime.vad_version,
            "yt_dlp_version": manifest.runtime.yt_dlp_version,
        },
        "runtime_rebuild_required": manifest.runtime_rebuild_required,
        "schedule_install_required": manifest.schedule_install_required,
        "schedule_local_time": manifest.schedule_local_time,
        "schema": manifest.schema,
        "source_tree_clean": manifest.source_tree_clean,
    }


def _manifest_from_object(value: object) -> TransferManifest:
    root = _require_object(
        value,
        {
            "branch",
            "bundle_id",
            "commit_sha",
            "created_at_utc",
            "credential_registration_required",
            "database",
            "members",
            "operator_state_destination",
            "remote_verified",
            "repository_url",
            "runtime",
            "runtime_rebuild_required",
            "schedule_install_required",
            "schedule_local_time",
            "schema",
            "source_tree_clean",
        },
    )
    database_raw = _require_object(
        root["database"],
        {
            "active_artifact_count",
            "integrity_check",
            "migrations",
            "reference_feature_count",
            "snapshot_sha256",
            "table_counts",
        },
    )
    runtime_raw = _require_object(
        root["runtime"],
        {
            "adapter_contract_version",
            "deno_version",
            "ffmpeg_version",
            "models",
            "provider",
            "python_version",
            "sherpa_onnx_version",
            "vad_contract_version",
            "vad_version",
            "yt_dlp_version",
        },
    )
    models_raw = _require_list(runtime_raw["models"])
    models = tuple(_parse_runtime_model(item) for item in models_raw)
    members_raw = _require_list(root["members"])
    members = tuple(_parse_member(item) for item in members_raw)
    migrations_raw = _require_list(database_raw["migrations"])
    table_counts_raw = _require_list(database_raw["table_counts"])
    migrations = tuple(_require_string(item) for item in migrations_raw)
    table_counts: list[tuple[str, int]] = []
    for item in table_counts_raw:
        values = _require_list(item)
        if len(values) != 2:
            raise ValueError("invalid table count")
        table_counts.append(
            (_require_string(values[0]), _require_integer(values[1]))
        )
    return TransferManifest(
        schema=_require_string(root["schema"]),
        bundle_id=_require_string(root["bundle_id"]),
        created_at_utc=_require_string(root["created_at_utc"]),
        repository_url=_require_string(root["repository_url"]),
        branch=_require_string(root["branch"]),
        commit_sha=_require_string(root["commit_sha"]),
        source_tree_clean=_require_boolean(root["source_tree_clean"]),
        remote_verified=_require_boolean(root["remote_verified"]),
        schedule_local_time=_require_string(root["schedule_local_time"]),
        runtime_rebuild_required=_require_boolean(
            root["runtime_rebuild_required"]
        ),
        credential_registration_required=_require_boolean(
            root["credential_registration_required"]
        ),
        schedule_install_required=_require_boolean(
            root["schedule_install_required"]
        ),
        operator_state_destination=_require_string(
            root["operator_state_destination"]
        ),
        database=DatabaseSummary(
            integrity_check=_require_string(
                database_raw["integrity_check"]
            ),
            migrations=migrations,
            table_counts=tuple(table_counts),
            reference_feature_count=_require_integer(
                database_raw["reference_feature_count"]
            ),
            active_artifact_count=_require_integer(
                database_raw["active_artifact_count"]
            ),
            snapshot_sha256=_require_string(
                database_raw["snapshot_sha256"]
            ),
        ),
        runtime=RuntimeSummary(
            python_version=_require_string(runtime_raw["python_version"]),
            sherpa_onnx_version=_require_string(
                runtime_raw["sherpa_onnx_version"]
            ),
            yt_dlp_version=_require_string(runtime_raw["yt_dlp_version"]),
            deno_version=_require_string(runtime_raw["deno_version"]),
            ffmpeg_version=_require_string(runtime_raw["ffmpeg_version"]),
            vad_version=_require_string(runtime_raw["vad_version"]),
            provider=_require_string(runtime_raw["provider"]),
            adapter_contract_version=_require_string(
                runtime_raw["adapter_contract_version"]
            ),
            vad_contract_version=_require_string(
                runtime_raw["vad_contract_version"]
            ),
            models=models,
        ),
        members=members,
    )


def _parse_runtime_model(value: object) -> RuntimeModel:
    item = _require_object(
        value,
        {
            "active",
            "lock_name",
            "model_member",
            "model_name",
            "model_version",
            "vad_member",
        },
    )
    return RuntimeModel(
        lock_name=_require_string(item["lock_name"]),
        model_name=_require_string(item["model_name"]),
        model_version=_require_string(item["model_version"]),
        model_member=_require_string(item["model_member"]),
        vad_member=_require_string(item["vad_member"]),
        active=_require_boolean(item["active"]),
    )


def _parse_member(value: object) -> BundleMember:
    item = _require_object(
        value,
        {"path", "role", "sha256", "size_bytes"},
    )
    return BundleMember(
        path=_require_string(item["path"]),
        role=_require_string(item["role"]),
        size_bytes=_require_integer(item["size_bytes"]),
        sha256=_require_string(item["sha256"]),
    )


def _require_object(
    value: object,
    keys: set[str],
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid object")
    return value


def _require_list(value: object) -> list[Any]:
    if type(value) is not list:
        raise ValueError("invalid list")
    return value


def _require_string(value: object) -> str:
    if type(value) is not str:
        raise ValueError("invalid string")
    return value


def _require_integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("invalid integer")
    return value


def _require_boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid boolean")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _is_commit_sha(value: object) -> bool:
    return type(value) is str and _COMMIT_SHA.fullmatch(value) is not None


def _is_token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _manifest_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_MANIFEST_INVALID",
        "transfer manifest is invalid",
    )


__all__ = [
    "BundleMember",
    "DatabaseSummary",
    "EXACT_MODEL_MEMBERS",
    "EXACT_PROJECT_WHEEL_MEMBER",
    "EXACT_REQUIREMENTS_MEMBER",
    "EXACT_RUNTIME_TOOL_MEMBERS",
    "EXACT_RUNTIME_WHEEL_MEMBERS",
    "MAX_MANIFEST_BYTES",
    "MAX_MEMBER_BYTES",
    "MAX_MEMBERS",
    "MAX_TOTAL_BYTES",
    "OPERATOR_STATE_DESTINATION",
    "RuntimeModel",
    "RuntimeSummary",
    "SCHEMA",
    "TransferManifest",
    "compute_bundle_id",
    "decode_manifest",
    "encode_manifest",
    "validate_member_path",
]
