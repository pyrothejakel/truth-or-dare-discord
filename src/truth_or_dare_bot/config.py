"""Validated configuration; importing this module never opens a database or logs secrets."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
import re


def clock_time(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Use a 24-hour time with two digits: HH:MM.")
    return value


def timezone_name(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Use a valid IANA timezone, such as America/Chicago.") from None
    return value


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    if not start or not end or start == end:
        return False
    current = now.strftime("%H:%M")
    return start <= current < end if start < end else current >= start or current < end


@dataclass(frozen=True)
class Config:
    guild_id: int
    channel_id: int
    timezone: str = "America/Chicago"
    post_time: str = "19:00"
    database: str = "data/truth_or_dare.sqlite3"

    @classmethod
    def from_env(cls):
        ids = []
        for key in ("GUILD_ID", "CHANNEL_ID"):
            value = os.getenv(key, "")
            if not value.isdecimal() or not 0 < int(value) < 2**64:
                raise ValueError(f"{key} must be a valid Discord ID.")
            ids.append(int(value))
        database = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PATH")
        if not database:
            database = str(Path(__file__).resolve().parents[2] / "data" / "truth_or_dare.sqlite3")
        if os.getenv("REQUIRE_POSTGRES", "false").lower() == "true" and not database.startswith(("postgres://", "postgresql://")):
            raise ValueError("Cloud deployment requires DATABASE_URL from the PostgreSQL addon.")
        return cls(*ids, timezone_name(os.getenv("TIMEZONE", "America/Chicago")),
                   clock_time(os.getenv("POST_TIME", "19:00")), database)
