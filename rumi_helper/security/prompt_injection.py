"""Prompt Injection 防御。

核心原则：网页内容一律是 UNTRUSTED DATA，不是 instruction。
这里提供统一的包装函数，任何网页内容送入 LLM 前都必须经过 wrap_untrusted_content。
总结链路永远拿不到 MessageDispatcher —— 网页内容在架构上不可能触发发送。
"""

import re

UNTRUSTED_OPEN = "<untrusted_web_content>"
UNTRUSTED_CLOSE = "</untrusted_web_content>"

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"(system|assistant)\s*(prompt|message)\s*[:,]?", re.IGNORECASE),
    re.compile(r"send\s+(a\s+)?(message|msg)\s+to", re.IGNORECASE),
    re.compile(r"发送\s*(消息|信息)\s*(到|给|至)", re.IGNORECASE),
    re.compile(r"(api[_\s-]?key|secret|password)\s*[:=]", re.IGNORECASE),
    re.compile(r"读取(本地)?文件|访问(其他|另一个)(网页|url)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
]


def wrap_untrusted_content(content: str) -> str:
    """用明确的定界符包裹网页内容，并中和内容中可能出现的定界符本身。"""
    neutralized = content.replace(UNTRUSTED_CLOSE, "[/untrusted_web_content]").replace(
        UNTRUSTED_OPEN, "[untrusted_web_content]"
    )
    return f"{UNTRUSTED_OPEN}\n{neutralized}\n{UNTRUSTED_CLOSE}"


def looks_like_injection(text: str) -> bool:
    """启发式检测（仅用于打日志告警，不阻断流程——阻断手段是定界符 + system prompt）。"""
    return any(p.search(text) for p in _INJECTION_PATTERNS)
