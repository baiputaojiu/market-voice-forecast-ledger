import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.audit import validate_audit_payload


@pytest.mark.parametrize(
    "key", ["text_body", "input_text", "audio_path", "embedding", "prompt_body"]
)
def test_audit_payload_rejects_private_body_keys(key):
    with pytest.raises(DomainError) as error:
        validate_audit_payload({key: "private"})
    assert error.value.code == "AUDIT_PRIVATE_FIELD"


@pytest.mark.parametrize(
    "payload",
    [
        {"change": {"input_text": "private"}},
        {"changes": [{"details": {"audio_path": "private"}}]},
    ],
)
def test_audit_payload_rejects_private_body_keys_recursively(payload):
    with pytest.raises(DomainError) as error:
        validate_audit_payload(payload)
    assert error.value.code == "AUDIT_PRIVATE_FIELD"


def test_audit_payload_allows_safe_structured_fields():
    validate_audit_payload(
        {
            "segment_id": 42,
            "body_hash": "synthetic-hash",
            "classification": {"direction": "up"},
            "evidence": ["short synthetic evidence"],
        }
    )
