from types import SimpleNamespace

import pytest

from rumi_helper.utils import local_reply_delay, send_local_reply


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
    assert sent == ["a" * 70]


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
    assert sent == ["立即回复"]
