from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    temp_audio_dir: Path

    @property
    def voice_runtime_dir(self) -> Path:
        return self.data_dir / "voice-runtime"

    @property
    def voice_model_dir(self) -> Path:
        return self.data_dir / "voice-models"

    @property
    def voice_work_dir(self) -> Path:
        return self.data_dir / "voice-work"

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> "Settings":
        return cls(data_dir, data_dir / "ledger.sqlite3", data_dir / "temp-audio")
