"""MessageDispatcher：全项目唯一的 QQ 消息发送出口。

安全约束：
- 只能被确定性的管理员命令逻辑调用；LLM / 网页总结链路拿不到本类引用。
- 每次发送写入 send_logs（目标、类型、时间、结果、message_id、错误）。
- broadcast 逐目标顺序发送并限速，单目标失败不中断。
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.database.db import Database
from app.services.qq.broadcast_parser import BroadcastTarget

logger = logging.getLogger(__name__)

# async (action: str, **params) -> dict；测试注入用
ApiCaller = Callable[..., Awaitable[dict]]


@dataclass
class SendResult:
    target_type: str
    target_id: str
    success: bool
    message_id: str | None = None
    error: str | None = None


@dataclass
class BroadcastReport:
    results: list[SendResult] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.success)

    def failed_lines(self) -> list[str]:
        return [f"{r.target_type}:{r.target_id}" for r in self.results if not r.success]

    def summary_text(self) -> str:
        lines = ["发送完成", f"成功：{self.success_count}", f"失败：{self.failed_count}"]
        failed = self.failed_lines()
        if failed:
            lines.append("失败目标：")
            lines.extend(failed)
        return "\n".join(lines)


class MessageDispatcher:
    def __init__(
        self,
        db: Database,
        rate_limit_per_second: float = 1.0,
        api_caller: ApiCaller | None = None,
        retry_delay_seconds: float = 30.0,
        account_pause_seconds: float = 1800.0,
        rate_limit_pause_seconds: float = 60.0,
        max_attempts: int = 3,
        queue_poll_seconds: float = 1.0,
        lease_seconds: float = 60.0,
    ):
        self._db = db
        self._interval = 1.0 / rate_limit_per_second if rate_limit_per_second and rate_limit_per_second > 0 else 0.0
        self._api_caller = api_caller
        self._retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._account_pause_seconds = max(0.0, account_pause_seconds)
        self._rate_limit_pause_seconds = max(0.0, rate_limit_pause_seconds)
        self._max_attempts = max(1, max_attempts)
        self._queue_poll_seconds = max(0.1, queue_poll_seconds)
        self._lease_seconds = max(1.0, lease_seconds)
        self._queue_task: asyncio.Task | None = None
        self._queue_wakeup = asyncio.Event()
        self._paused_until = 0.0
        self._stopping = False

    async def enqueue_user(self, user_id: str, message: str, max_attempts: int | None = None) -> int:
        return await self._enqueue("user", str(user_id), message, max_attempts)

    async def enqueue_group(self, group_id: str, message: str, max_attempts: int | None = None) -> int:
        return await self._enqueue("group", str(group_id), message, max_attempts)

    async def _enqueue(self, target_type: str, target_id: str, message: str, max_attempts: int | None) -> int:
        remaining_pause = max(0.0, self._paused_until - time.monotonic())
        not_before = None
        if remaining_pause > 0:
            not_before = (datetime.now(UTC) + timedelta(seconds=remaining_pause)).strftime("%Y-%m-%d %H:%M:%S")
        message_id = await self._db.enqueue_outbound_message(
            target_type,
            target_id,
            message,
            max(1, max_attempts or self._max_attempts),
            not_before=not_before,
        )
        self._queue_wakeup.set()
        return message_id

    async def start(self) -> None:
        if self._queue_task is None:
            self._stopping = False
            self._queue_task = asyncio.create_task(self._queue_loop(), name="outbound-message-worker")

    async def stop(self) -> None:
        """Finish the current delivery before cancelling as a bounded fallback."""
        task = self._queue_task
        if task is None:
            return
        self._stopping = True
        self._queue_wakeup.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except TimeoutError:
            logger.warning("主动消息队列优雅关闭超时，取消 worker")
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        finally:
            self._queue_task = None

    async def _queue_loop(self) -> None:
        while not self._stopping:
            try:
                await self.process_outbound_messages()
            except Exception:
                logger.exception("主动消息队列处理失败")
            if self._stopping:
                break
            self._queue_wakeup.clear()
            try:
                await asyncio.wait_for(self._queue_wakeup.wait(), timeout=self._queue_poll_seconds)
            except TimeoutError:
                pass

    async def process_outbound_messages(self, limit: int = 20) -> int:
        if await self._remaining_pause_seconds() > 0:
            return 0
        processed = 0
        for _ in range(max(1, limit)):
            now = datetime.now(UTC)
            row = await self._db.claim_outbound_message(
                now.strftime("%Y-%m-%d %H:%M:%S"),
                (now - timedelta(seconds=self._lease_seconds)).strftime("%Y-%m-%d %H:%M:%S"),
            )
            if row is None:
                break
            result = await self._send(str(row["target_type"]), str(row["target_id"]), str(row["content"]))
            processed += 1
            if result.success:
                await self._db.mark_outbound_message_sent(
                    int(row["id"]), datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"), result.message_id
                )
            else:
                pause_seconds = await self._remaining_pause_seconds()
                retry_at = datetime.now(UTC) + timedelta(
                    seconds=max(self._retry_delay_seconds, pause_seconds)
                )
                retry_at_text = retry_at.strftime("%Y-%m-%d %H:%M:%S")
                await self._db.retry_outbound_message(
                    int(row["id"]),
                    int(row["attempts"]),
                    int(row["max_attempts"]),
                    retry_at_text,
                    result.error or "发送失败",
                )
                if pause_seconds > 0:
                    # Persist the cooldown on every queued item. The in-memory
                    # circuit breaker is lost on restart, but next_attempt_at is not.
                    await self._db.defer_outbound_messages(retry_at_text)
                    break
            if self._stopping:
                break
        return processed

    async def _call_api(self, action: str, **params) -> dict:
        if self._api_caller is not None:
            return await self._api_caller(action, **params)
        from nonebot import get_bot

        bot = get_bot()
        return await bot.call_api(action, **params)

    async def send_user(self, user_id: str, message: str) -> SendResult:
        return await self._send("user", str(user_id), message)

    async def send_group(self, group_id: str, message: str) -> SendResult:
        return await self._send("group", str(group_id), message)

    async def _send(self, target_type: str, target_id: str, message: str) -> SendResult:
        if await self._remaining_pause_seconds() > 0:
            result = SendResult(target_type, target_id, False, error="主动发送已暂停，等待账号/API恢复")
            await self._log(result, message)
            return result
        try:
            numeric_id = int(target_id)
        except ValueError as e:
            result = SendResult(target_type, target_id, False, error=f"无效的 QQ/群号：{target_id}")
            logger.warning("发送失败 %s:%s %s", target_type, target_id, e)
            await self._log(result, message)
            return result

        action = "send_private_msg" if target_type == "user" else "send_group_msg"
        params: dict = {"message": message, **({"user_id": numeric_id} if target_type == "user" else {"group_id": numeric_id})}
        try:
            resp = await self._call_api(action, **params)
            mid = resp.get("message_id") if isinstance(resp, dict) else None
            result = SendResult(target_type, target_id, True, message_id=str(mid) if mid is not None else None)
            logger.info("已发送 type=%s id=%s message_id=%s", target_type, target_id, result.message_id)
        except Exception as e:  # noqa: BLE001 —— 单目标失败必须被记录，不向外扩散
            result = SendResult(target_type, target_id, False, error=f"{type(e).__name__}: {e}")
            logger.warning("发送失败 type=%s id=%s %s", target_type, target_id, e)

        if not result.success:
            pause_seconds = self._pause_for_error(result.error)
            if pause_seconds > 0:
                await self._persist_pause(pause_seconds)
        await self._log(result, message)
        return result

    async def broadcast(self, targets: list[BroadcastTarget], message: str) -> BroadcastReport:
        report = BroadcastReport()
        for i, t in enumerate(targets):
            if i > 0 and self._interval > 0:
                await asyncio.sleep(self._interval)  # 限速，禁止瞬时并发轰炸
            report.results.append(await self._send(t.type, t.id, message))
            if await self._remaining_pause_seconds() > 0:
                logger.warning("主动群发因账号/API异常暂停，已停止继续发送")
                break
        return report

    async def _remaining_pause_seconds(self) -> float:
        """Merge the in-memory deadline with the persisted global cooldown."""
        remaining = max(0.0, self._paused_until - time.monotonic())
        paused_until = await self._db.fetch_outbound_pause_until()
        try:
            persisted = datetime.strptime(paused_until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            persisted_remaining = max(0.0, (persisted - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            persisted_remaining = 0.0
        if persisted_remaining > remaining:
            self._paused_until = time.monotonic() + persisted_remaining
            remaining = persisted_remaining
        return remaining

    async def _persist_pause(self, pause_seconds: float) -> None:
        if pause_seconds <= 0:
            return
        paused_until = (datetime.now(UTC) + timedelta(seconds=pause_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.extend_outbound_pause_until(paused_until)
        await self._db.defer_outbound_messages(paused_until)

    def _pause_for_error(self, error: str | None) -> float:
        """账号失效或限流时熔断主动发送，避免重试造成更大压力。"""
        normalized = (error or "").lower()
        if any(marker in normalized for marker in (
            "kickedoffline", "登录态已失效", "请重新登录", "账号当前登录已失效", "账号状态变更为离线",
        )):
            pause_seconds = self._account_pause_seconds
            reason = "账号登录态失效/离线"
        elif any(marker in normalized for marker in (
            "429", "rate limit", "too many requests", "频繁", "限流",
        )):
            pause_seconds = self._rate_limit_pause_seconds
            reason = "接口限流"
        else:
            return 0.0
        if pause_seconds <= 0:
            return 0.0
        now = time.monotonic()
        if self._paused_until <= now:
            self._paused_until = now + pause_seconds
            logger.warning("主动发送熔断 reason=%s pause_seconds=%s", reason, int(pause_seconds))
        return max(0.0, self._paused_until - now)

    async def _log(self, result: SendResult, content: str) -> None:
        try:
            await self._db.insert_send_log(
                result.target_type, result.target_id, content, result.success, result.message_id, result.error
            )
        except Exception:
            logger.exception("写入 send_logs 失败")
