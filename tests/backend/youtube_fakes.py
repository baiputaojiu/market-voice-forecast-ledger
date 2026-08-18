from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from market_voice_forecast_ledger.domain.errors import DomainError


FIXED_NOW = datetime(2026, 8, 18, 2, 3, 4, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED_NOW


class FakeCredentialStore:
    def __init__(
        self,
        secret: str = "synthetic-youtube-key-000001",
        *,
        read_error: BaseException | None = None,
    ) -> None:
        self._secret = secret
        self._read_error = read_error
        self.read_count = 0

    def set_api_key(self, secret: str) -> None:
        self._secret = secret

    def has_api_key(self) -> bool:
        return self._read_error is None

    def read_api_key(self) -> str:
        self.read_count += 1
        if self._read_error is not None:
            raise self._read_error
        return self._secret

    def delete_api_key(self) -> bool:
        return True


class FakeYouTubeTransport:
    def __init__(
        self,
        *,
        page: Mapping[str, object] | None = None,
        responses: tuple[Mapping[str, object] | BaseException, ...] | None = None,
        events: list[str] | None = None,
    ) -> None:
        default_page: Mapping[str, object] = {"items": [], "nextPageToken": None}
        self._responses = list(
            responses if responses is not None else (
                page if page is not None else default_page,
            )
        )
        self.safe_requests: list[dict[str, str]] = []
        self.api_key_was_supplied: list[bool] = []
        self.events = events

    def get_json(
        self,
        endpoint: str,
        params: Mapping[str, str],
        api_key: str,
    ) -> Mapping[str, object]:
        request = {"endpoint": endpoint, **dict(params)}
        self.safe_requests.append(request)
        self.api_key_was_supplied.append(bool(api_key))
        if self.events is not None:
            self.events.append("transport")
        if not self._responses:
            raise AssertionError("fake transport received an unexpected request")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class RecordingReservation:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[object, int, datetime]] = []
        self.events = events

    def __call__(
        self,
        endpoint_class: object,
        attempt_no: int,
        attempted_at: datetime,
    ) -> None:
        self.calls.append((endpoint_class, attempt_no, attempted_at))
        if self.events is not None:
            self.events.append(f"reserve:{attempt_no}")


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeUrlResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.closed = False

    def __enter__(self) -> "FakeUrlResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.closed = True

    def read(self, limit: int = -1) -> bytes:
        if limit < 0:
            return self._body
        return self._body[:limit]


def missing_credential_error() -> DomainError:
    return DomainError(
        "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
        "YouTube credential is not configured",
    )
