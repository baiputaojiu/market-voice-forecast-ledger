from collections.abc import Iterator, Mapping

from market_voice_forecast_ledger.domain.errors import DomainError


FORBIDDEN_AUDIT_KEYS = {
    "text_body",
    "input_text",
    "audio_path",
    "embedding",
    "prompt_body",
}


def validate_audit_payload(value: object) -> None:
    for key in walk_mapping_keys(value):
        if key in FORBIDDEN_AUDIT_KEYS:
            raise DomainError(
                "AUDIT_PRIVATE_FIELD", f"forbidden audit key: {key}"
            )


def walk_mapping_keys(value: object) -> Iterator[object]:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            yield key
            yield from walk_mapping_keys(nested_value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from walk_mapping_keys(item)
