import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone


JST = timezone(timedelta(hours=9), name="JST")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_jst(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(JST)


def cutoff_exclusive_utc(day: date) -> datetime:
    next_midnight_jst = datetime.combine(day + timedelta(days=1), time.min, tzinfo=JST)
    return next_midnight_jst.astimezone(timezone.utc)
