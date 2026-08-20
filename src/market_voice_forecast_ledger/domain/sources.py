from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SubjectRecord:
    id: int
    canonical_name: str
    is_active: bool
    created_at: datetime
    aliases: tuple[str, ...] = ()
