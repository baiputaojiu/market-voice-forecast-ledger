from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    temp_audio_dir: Path

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "Settings":
        return cls(data_dir, data_dir / "ledger.sqlite3", data_dir / "temp-audio")
