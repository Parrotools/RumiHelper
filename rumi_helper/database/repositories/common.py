"""Small helpers shared by repository mixins."""

from datetime import UTC, datetime

from rumi_helper.utils import truncate_for_qq


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _outbound_content(value: object) -> str:
    """Normalize every durable outbound body to the QQ-safe single-message limit."""
    return truncate_for_qq(str(value))
