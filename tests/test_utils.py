import asyncio
from types import SimpleNamespace

import pytest

from rumi_helper.utils import (
    QQ_TEXT_SAFE_LIMIT,
    ReplyGate,
    local_reply_delay,
    send_local_reply,
    send_reply,
    truncate_for_qq,
)


def test_local_reply_delay_scales_with_text_and_is_capped():
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            non_llm_reply_delay_min_seconds=0.5,
            non_llm_reply_delay_max_seconds=4.0,
            non_llm_reply_delay_chars_per_second=35.0,
        )
    )

    assert local_reply_delay(runtime, "短") == 0.5
    assert local_reply_delay(runtime, "a" * 70) == 2.0
    assert local_reply_delay(runtime, "a" * 500) == 4.0


@pytest.mark.asyncio
async def test_send_local_reply_uses_dynamic_delay(monkeypatch):
    delays: list[float] = []
    sent: list[str] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    class FakeMatcher:
        async def send(self, message: str) -> None:
            sent.append(message)

    monkeypatch.setattr("rumi_helper.utils.asyncio.sleep", fake_sleep)

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            non_llm_reply_delay_min_seconds=0.5,
            non_llm_reply_delay_max_seconds=4.0,
            non_llm_reply_delay_chars_per_second=35.0,
        )
    )
    await send_local_reply(FakeMatcher(), runtime, "a" * 70)

    assert delays == [2.0]
    assert len(sent) == 1
    assert sent[0].type == "text"
    assert sent[0].data["text"] == "a" * 70


def test_truncate_for_qq_respects_total_limit():
    text = "a" * (QQ_TEXT_SAFE_LIMIT + 500)
    truncated = truncate_for_qq(text)

    assert len(truncated) <= QQ_TEXT_SAFE_LIMIT
    assert "内容过长已截断" in truncated
    assert str(len(text)) in truncated


@pytest.mark.asyncio
async def test_reply_gate_cancelled_waiter_does_not_reserve_future_slot(monkeypatch):
    gate = ReplyGate()
    gate._next_send_at = 10.0

    monkeypatch.setattr("rumi_helper.utils.time.monotonic", lambda: 9.0)

    async def cancelled_sleep(delay: float) -> None:
        assert delay == 1.0
        raise asyncio.CancelledError

    monkeypatch.setattr("rumi_helper.utils.asyncio.sleep", cancelled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await gate.wait_for_slot(1.0)

    assert gate._next_send_at == 10.0


@pytest.mark.asyncio
async def test_send_reply_applies_global_qq_text_limit():
    sent: list[str] = []

    class FakeMatcher:
        async def send(self, message: str) -> None:
            sent.append(message)

    runtime = SimpleNamespace(
        settings=SimpleNamespace(interactive_send_rate_limit_per_second=0)
    )
    await send_reply(FakeMatcher(), runtime, "x" * (QQ_TEXT_SAFE_LIMIT + 100))

    assert len(sent) == 1
    assert sent[0].type == "text"
    assert len(sent[0].data["text"]) <= QQ_TEXT_SAFE_LIMIT
    assert "内容过长已截断" in sent[0].data["text"]


@pytest.mark.asyncio
async def test_send_local_reply_can_be_disabled(monkeypatch):
    delays: list[float] = []
    sent: list[str] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    class FakeMatcher:
        async def send(self, message: str) -> None:
            sent.append(message)

    monkeypatch.setattr("rumi_helper.utils.asyncio.sleep", fake_sleep)

    runtime = SimpleNamespace(settings=SimpleNamespace(non_llm_reply_delay_max_seconds=0))
    await send_local_reply(FakeMatcher(), runtime, "立即回复")

    assert delays == []
    assert len(sent) == 1
    assert sent[0].type == "text"
    assert sent[0].data["text"] == "立即回复"


@pytest.mark.asyncio
async def test_send_reply_keeps_cq_codes_as_literal_text():
    sent = []

    class FakeMatcher:
        async def send(self, message) -> None:
            sent.append(message)

    runtime = SimpleNamespace(
        settings=SimpleNamespace(interactive_send_rate_limit_per_second=0)
    )
    payload = "[CQ:at,qq=all] [CQ:image,file=https://example.com/x]"

    await send_reply(FakeMatcher(), runtime, payload)

    assert len(sent) == 1
    assert sent[0].type == "text"
    assert sent[0].data["text"] == payload
