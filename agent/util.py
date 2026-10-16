"""Small time / id helpers shared across the agent."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().replace(microsecond=0).isoformat()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hours_since(start_iso: str, now: datetime | None = None) -> float:
    now = now or utcnow()
    return (now - parse_iso(start_iso)).total_seconds() / 3600.0


def add_hours(start: datetime, hours: float) -> datetime:
    return start + timedelta(hours=hours)


def new_id(prefix: str = "") -> str:
    token = uuid.uuid4().hex[:12]
    return f"{prefix}{token}" if prefix else token


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
