import re
import sqlite3
import unicodedata
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Final

from market_voice_forecast_ledger.domain.common import sha256_text
from market_voice_forecast_ledger.domain.errors import DomainError


FORBIDDEN_AUDIT_KEYS = {
    "text_body",
    "input_text",
    "audio_path",
    "embedding",
    "prompt_body",
}

_SAFE_AUDIT_TOKEN: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
)
_ABSOLUTE_PATH: Final = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9])[a-z]:[\\/]"
    r"|(?<![\\/])(?:\\\\|//)[^\\/\s]"
    r"|(?<![A-Za-z0-9/])/(?!/)[^/\s])"
)


def validate_audit_payload(value: object) -> None:
    for key in walk_mapping_keys(value):
        if key in FORBIDDEN_AUDIT_KEYS:
            raise DomainError(
                "AUDIT_PRIVATE_FIELD", f"forbidden audit key: {key}"
            )


def validate_audit_scalars(
    *,
    entity_type: object,
    entity_id: object,
    scope_id: object,
    operation: object,
    actor_kind: object,
    reason_code: object,
    created_at: object,
) -> None:
    tokens = (entity_type, entity_id, operation, reason_code)
    if any(
        type(value) is not str or _SAFE_AUDIT_TOKEN.fullmatch(value) is None
        for value in tokens
    ):
        _raise_invalid_scalar()
    if scope_id is not None and (
        type(scope_id) is not int or scope_id <= 0
    ):
        _raise_invalid_scalar()
    if type(actor_kind) is not str or actor_kind not in {"user", "system"}:
        _raise_invalid_scalar()
    if (
        type(created_at) is not datetime
        or created_at.tzinfo is None
        or created_at.utcoffset() is None
    ):
        _raise_invalid_scalar()


def validate_audit_reason(conn: sqlite3.Connection, reason: object) -> None:
    if type(reason) is not str:
        _raise_invalid_scalar()
    stripped = reason.strip()
    if not stripped:
        _raise_invalid_scalar()
    if len(reason) > 256:
        _raise_private_reason()
    if any(unicodedata.category(character).startswith("C") for character in reason):
        _raise_private_reason()
    if _ABSOLUTE_PATH.search(reason) is not None or "file://" in reason.casefold():
        _raise_private_reason()
    lowered = reason.casefold()
    if any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", lowered)
        for key in FORBIDDEN_AUDIT_KEYS
    ):
        _raise_private_reason()

    reason_hash = sha256_text(reason)
    stripped_hash = sha256_text(stripped)
    private_match = conn.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM transcript_segments
                WHERE text_body IS NOT NULL
                    AND length(text_body) > 0
                    AND (instr(?, text_body) > 0 OR instr(?, text_body) > 0)
            )
            OR EXISTS (
                SELECT 1
                FROM analysis_input_snapshots
                WHERE input_text IS NOT NULL
                    AND length(input_text) > 0
                    AND (instr(?, input_text) > 0 OR instr(?, input_text) > 0)
            )
            OR EXISTS (
                SELECT 1
                FROM transcript_segments
                WHERE text_sha256 IN (?, ?)
            )
            OR EXISTS (
                SELECT 1
                FROM analysis_input_snapshots
                WHERE input_sha256 IN (?, ?)
            )
        """,
        (
            reason,
            stripped,
            reason,
            stripped,
            reason_hash,
            stripped_hash,
            reason_hash,
            stripped_hash,
        ),
    ).fetchone()[0]
    if private_match:
        _raise_private_reason()


def _raise_invalid_scalar() -> None:
    raise DomainError(
        "AUDIT_SCALAR_INVALID",
        "audit metadata has an invalid scalar shape",
    )


def _raise_private_reason() -> None:
    raise DomainError(
        "AUDIT_REASON_PRIVATE",
        "audit reason contains prohibited private content",
    )


def walk_mapping_keys(value: object) -> Iterator[object]:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            yield key
            yield from walk_mapping_keys(nested_value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from walk_mapping_keys(item)
