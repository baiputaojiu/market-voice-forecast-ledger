import sqlite3

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    PolicyKind,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.sources import ChannelPolicy
from market_voice_forecast_ledger.repositories.sources import SourceRepository


def bootstrap_reference_data(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        repo = SourceRepository(conn)
        kinouchi_id = repo.create_subject(
            "木野内栄治", SubjectKind.PERSON, aliases=("木野内英二",)
        )
        akatsuki_id = repo.create_subject("暁投資顧問", SubjectKind.ORGANIZATION)
        emori_id = repo.create_subject("江守哲", SubjectKind.PERSON)
        okawa_id = repo.create_subject(
            "大川智宏", SubjectKind.PERSON, aliases=("大川智ひろ",)
        )

        repo.create_policy(
            kinouchi_id,
            ChannelPolicy(
                policy_kind=PolicyKind.ALL_CHANNELS,
                configuration_status=ConfigurationStatus.CONFIGURED,
            ),
        )
        repo.create_policy(
            akatsuki_id,
            ChannelPolicy(
                policy_kind=PolicyKind.FIXED_CHANNEL,
                configuration_status=ConfigurationStatus.CONFIGURED,
                youtube_channel_id="UCOfzLmXpI3qmZfV7_Cs1sYA",
                channel_display_name="暁投資顧問",
            ),
        )
        repo.create_policy(
            emori_id,
            ChannelPolicy(
                policy_kind=PolicyKind.FIXED_CHANNEL,
                configuration_status=ConfigurationStatus.CONFIGURED,
                youtube_channel_id="UCVXka7buS_WptsAzSE0LcKg",
                channel_display_name="江守哲の米国株投資チャンネル",
            ),
        )
        repo.create_policy(
            okawa_id,
            ChannelPolicy(
                policy_kind=PolicyKind.ALL_CHANNELS,
                configuration_status=ConfigurationStatus.CONFIGURED,
            ),
        )
