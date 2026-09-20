"""Identifier validation shared by configuration and message-facing services."""


def is_ascii_decimal_id(value: object) -> bool:
    """Return True only for positive, non-empty ASCII decimal identifiers."""
    text = str(value)
    return (
        bool(text)
        and text.isascii()
        and text.isdecimal()
        and any(char != "0" for char in text)
    )
