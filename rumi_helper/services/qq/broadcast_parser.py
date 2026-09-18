"""broadcast 命令解析器（纯逻辑，便于单测）。

命令格式：
    /broadcast user:123,user:456,group:789 -- 消息内容
"""

from dataclasses import dataclass

COMMAND_PREFIX = "/broadcast"
SEPARATOR = "--"
VALID_TARGET_TYPES = ("user", "group")


class BroadcastFormatError(ValueError):
    pass


@dataclass(frozen=True)
class BroadcastTarget:
    type: str  # "user" | "group"
    id: str

    def display(self) -> str:
        return f"{self.type}:{self.id}"

    def to_dict(self) -> dict:
        return {"type": self.type, "id": self.id}


def parse_targets(raw: str) -> list[BroadcastTarget]:
    targets: list[BroadcastTarget] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        type_, sep, target_id = part.partition(":")
        type_ = type_.strip().lower()
        target_id = target_id.strip()
        if not sep or type_ not in VALID_TARGET_TYPES:
            raise BroadcastFormatError(f"无效目标「{part}」，应为 user:QQ号 或 group:群号")
        if not target_id.isdigit():
            raise BroadcastFormatError(f"无效的 QQ/群号「{target_id}」")
        targets.append(BroadcastTarget(type_, target_id))
    if not targets:
        raise BroadcastFormatError("至少需要一个发送目标")
    return targets


def parse_broadcast_command(text: str) -> tuple[list[BroadcastTarget], str]:
    """解析整条命令文本，返回 (targets, message)。失败抛 BroadcastFormatError。"""
    body = text.strip()
    if body.lower().startswith(COMMAND_PREFIX):
        body = body[len(COMMAND_PREFIX):]
    body = body.strip()
    if SEPARATOR not in body:
        raise BroadcastFormatError("格式：/broadcast user:123,group:456 -- 消息内容")
    targets_raw, _, message = body.partition(SEPARATOR)
    message = message.strip()
    if not message:
        raise BroadcastFormatError("消息内容不能为空")
    return parse_targets(targets_raw), message
