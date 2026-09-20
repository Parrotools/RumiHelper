import pytest

from rumi_helper.services.qq.broadcast_parser import (
    BroadcastFormatError,
    parse_broadcast_command,
    parse_targets,
)


def test_parse_valid():
    targets, message = parse_broadcast_command("/broadcast user:123,user:456,group:789 -- 今晚会议取消")
    assert [(t.type, t.id) for t in targets] == [("user", "123"), ("user", "456"), ("group", "789")]
    assert message == "今晚会议取消"


def test_parse_message_with_separator_chars():
    targets, message = parse_broadcast_command("/broadcast group:789 -- 注意：a -- b")
    assert message == "注意：a -- b"
    assert targets[0].id == "789"


def test_missing_separator():
    with pytest.raises(BroadcastFormatError):
        parse_broadcast_command("/broadcast user:123 hello")


def test_empty_message():
    with pytest.raises(BroadcastFormatError):
        parse_broadcast_command("/broadcast user:123 --   ")


def test_invalid_target_type():
    with pytest.raises(BroadcastFormatError):
        parse_targets("channel:123")


def test_invalid_target_id():
    with pytest.raises(BroadcastFormatError):
        parse_targets("user:abc")


def test_empty_targets():
    with pytest.raises(BroadcastFormatError):
        parse_targets("  ")


def test_rejects_oversized_message():
    with pytest.raises(BroadcastFormatError, match="4000"):
        parse_broadcast_command(
            "/broadcast user:123 -- " + ("x" * 4001)
        )


@pytest.mark.parametrize("target", ["user:²", "user:１２３", "group:①"])
def test_rejects_non_ascii_numeric_target_ids(target):
    with pytest.raises(BroadcastFormatError):
        parse_targets(target)
