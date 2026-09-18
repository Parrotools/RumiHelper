"""Small helpers shared by repository mixins."""

from datetime import UTC, datetime


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
