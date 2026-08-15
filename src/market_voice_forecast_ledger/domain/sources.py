from dataclasses import dataclass
from datetime import datetime

from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    PolicyKind,
    SubjectKind,
)


@dataclass(frozen=True, slots=True)
class VideoInput:
    youtube_video_id: str
    youtube_channel_id: str | None
    channel_display_name: str
    title: str
    published_at: datetime
    duration_seconds: int
    live_kind: str


@dataclass(frozen=True, slots=True)
class VideoRecord:
    id: int
    youtube_video_id: str
    youtube_channel_id: str | None
    channel_display_name: str
    title: str
    published_at: datetime
    duration_seconds: int
    live_kind: str


@dataclass(frozen=True, slots=True)
class ChannelPolicy:
    policy_kind: PolicyKind
    configuration_status: ConfigurationStatus
    youtube_channel_id: str | None = None
    channel_display_name: str | None = None
    id: int | None = None
    subject_id: int | None = None
    policy_hash: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubjectRecord:
    id: int
    canonical_name: str
    subject_kind: SubjectKind
    is_active: bool
    aliases: tuple[str, ...] = ()
