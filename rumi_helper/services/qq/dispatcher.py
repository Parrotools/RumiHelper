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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from rumi_helper.database.db import Database
from rumi_helper.services.qq.broadcast_parser import BroadcastTarget
from rumi_helper.utils import truncate_for_qq

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
    skipped_targets: list[BroadcastTarget] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.success)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_targets)

    def failed_lines(self) -> list[str]:
        return [f"{r.target_type}:{r.target_id}" for r in self.results if not r.success]

    def skipped_lines(self) -> list[str]:
        return [f"{target.type}:{target.id}" for target in self.skipped_targets]

    def summary_text(self) -> str:
        lines = [
            "发送已停止" if self.skipped_targets else "发送完成",
            f"成功：{self.success_count}",
            f"失败：{self.failed_count}",
        ]
        failed = self.failed_lines()
        if failed:
            lines.append("失败目标：")
            lines.extend(failed)
        skipped = self.skipped_lines()
        if skipped:
            lines.append(f"未发送：{self.skipped_count}")
            lines.extend(skipped)
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
        shutdown_timeout_seconds: float = 10.0,
        terminal_history_limit: int = 5000,
        send_log_history_limit: int = 5000,
    ):
        self._db = db
        self._interval = 1.0 / rate_limit_per_second if rate_limit_per_second and rate_limit_per_second > 0 else 0.0
        self._send_lock = asyncio.Lock()
        self._api_caller = api_caller
        self._retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._account_pause_seconds = max(0.0, account_pause_seconds)
        self._rate_limit_pause_seconds = max(0.0, rate_limit_pause_seconds)
        self._max_attempts = max(1, max_attempts)
        self._queue_poll_seconds = max(0.1, queue_poll_seconds)
        self._lease_seconds = max(1.0, lease_seconds)
        self._shutdown_timeout_seconds = max(0.1, float(shutdown_timeout_seconds))
        self._shutdown_cancel_grace_seconds = min(1.0, self._shutdown_timeout_seconds)
        self._terminal_history_limit = max(0, int(terminal_history_limit))
        self._send_log_history_limit = max(0, int(send_log_history_limit))
        self._queue_task: asyncio.Task | None = None
        self._queue_wakeup = asyncio.Event()
        self._paused_until = 0.0
        self._stopping = False

    async def enqueue_user(self, user_id: str, message: str, max_attempts: int | None = None) -> int:
        return await self._enqueue("user", str(user_id), message, max_attempts)

    async def enqueue_group(self, group_id: str, message: str, max_attempts: int | None = None) -> int:
        return await self._enqueue("group", str(group_id), message, max_attempts)

    @property
    def worker_running(self) -> bool:
        task = self._queue_task
        return (
            task is not None
            and not task.done()
            and not self._stopping
        )

    def notify_queue_changed(self) -> None:
        """Wake the durable outbound worker after another atomic DB workflow queued rows."""
        self._queue_wakeup.set()

    async def _enqueue(self, target_type: str, target_id: str, message: str, max_attempts: int | None) -> int:
        message = truncate_for_qq(str(message))
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
            await self._prune_terminal_history()
            await self._prune_send_log_history()
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
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("主动消息队列优雅关闭超时，取消 worker")
            task.cancel()
            _done, pending = await asyncio.wait(
                {task},
                timeout=self._shutdown_cancel_grace_seconds,
            )
            if pending:
                logger.error(
                    "主动消息 worker 未响应取消，继续关闭；任务可能在进程退出前仍短暂运行"
                )
                task.cancel()
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
                self._lease_timestamp(now),
                self._lease_timestamp(now - timedelta(seconds=self._lease_seconds)),
            )
            if row is None:
                break

            message_id = int(row["id"])
            lease_token = str(row.get("lease_token") or "")
            if not lease_token:
                logger.error("主动消息领取缺少 lease token message_id=%s", message_id)
                break

            heartbeat_stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(message_id, lease_token, heartbeat_stop),
                name=f"outbound-lease-{message_id}",
            )
            try:
                notification_kind = str(row.get("notification_kind") or "")
                notification_owner_id = str(row.get("notification_owner_id") or "")
                if notification_kind and notification_owner_id:
                    enabled = await self._db.outbound_notification_enabled(
                        notification_owner_id,
                        notification_kind,
                    )
                    if not enabled:
                        discarded = await self._db.discard_claimed_outbound_message(
                            message_id,
                            lease_token,
                        )
                        if discarded:
                            processed += 1
                            logger.info(
                                "通知已关闭，丢弃待发送消息 message_id=%s kind=%s owner=%s",
                                message_id,
                                notification_kind,
                                notification_owner_id,
                            )
                        else:
                            logger.warning(
                                "通知关闭后丢弃失败，lease 已转移 message_id=%s",
                                message_id,
                            )
                        continue

                result = await self._send(
                    str(row["target_type"]),
                    str(row["target_id"]),
                    str(row["content"]),
                    notification_owner_id=notification_owner_id or None,
                    notification_kind=notification_kind or None,
                )
                processed += 1
                owned = False
                if result.error == "NotificationDisabled":
                    owned = await self._db.discard_claimed_outbound_message(
                        message_id,
                        lease_token,
                    )
                elif result.success:
                    owned = await self._db.mark_outbound_message_sent(
                        message_id,
                        self._lease_timestamp(datetime.now(UTC)),
                        result.message_id,
                        lease_token=lease_token,
                    )
                else:
                    pause_seconds = await self._remaining_pause_seconds()
                    retry_at = datetime.now(UTC) + timedelta(
                        seconds=max(self._retry_delay_seconds, pause_seconds)
                    )
                    retry_at_text = retry_at.strftime("%Y-%m-%d %H:%M:%S")
                    owned = await self._db.retry_outbound_message(
                        message_id,
                        int(row["attempts"]),
                        int(row["max_attempts"]),
                        retry_at_text,
                        result.error or "发送失败",
                        lease_token=lease_token,
                    )
                    if owned and pause_seconds > 0:
                        # Persist the cooldown on every queued item. The in-memory
                        # circuit breaker is lost on restart, but next_attempt_at is not.
                        await self._db.defer_outbound_messages(retry_at_text)

                if not owned:
                    logger.warning(
                        "主动消息 lease 已转移，忽略旧 worker 的终态更新 message_id=%s",
                        message_id,
                    )
                await self._prune_terminal_history()
                if self._stopping:
                    break
            finally:
                heartbeat_stop.set()
                await asyncio.gather(heartbeat, return_exceptions=True)
        return processed

    @staticmethod
    def _lease_timestamp(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")

    async def _lease_heartbeat(
        self,
        message_id: int,
        lease_token: str,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.2, self._lease_seconds / 3.0)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await self._db.renew_outbound_message_lease(
                    message_id,
                    lease_token,
                    self._lease_timestamp(datetime.now(UTC)),
                )
                if not renewed:
                    return
            except Exception as exc:  # noqa: BLE001 - a later heartbeat may recover
                logger.warning(
                    "主动消息 lease 续租失败 message_id=%s error=%s",
                    message_id,
                    type(exc).__name__,
                )

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

    async def _send(
        self,
        target_type: str,
        target_id: str,
        message: str,
        *,
        notification_owner_id: str | None = None,
        notification_kind: str | None = None,
    ) -> SendResult:
        message = truncate_for_qq(str(message))
        try:
            numeric_id = int(target_id)
        except ValueError:
            result = SendResult(target_type, target_id, False, error="InvalidTarget")
            logger.warning("发送失败 type=%s id=%s error=InvalidTarget", target_type, target_id)
            await self._log(result, message)
            return result

        action = "send_private_msg" if target_type == "user" else "send_group_msg"
        params: dict = {
            "message": message,
            "auto_escape": True,
            **(
                {"user_id": numeric_id}
                if target_type == "user"
                else {"group_id": numeric_id}
            ),
        }

        # Every active send path (direct send, broadcast, durable queue) converges
        # here. Serialize them so SEND_RATE_LIMIT_PER_SECOND is a process-wide
        # database-wide outbound limit instead of a per-broadcast delay.
        async with self._send_lock:
            result: SendResult | None = None
            if await self._remaining_pause_seconds() > 0:
                result = SendResult(
                    target_type,
                    target_id,
                    False,
                    error="主动发送已暂停，等待账号/API恢复",
                )
            else:
                if self._interval > 0:
                    await self._wait_for_global_send_slot()
                    # A different process may have persisted a cooldown while
                    # this sender waited for its slot.
                    if await self._remaining_pause_seconds() > 0:
                        result = SendResult(
                            target_type,
                            target_id,
                            False,
                            error="主动发送已暂停，等待账号/API恢复",
                        )

                if result is None and notification_owner_id and notification_kind:
                    enabled = await self._db.outbound_notification_enabled(
                        notification_owner_id,
                        notification_kind,
                    )
                    if not enabled:
                        return SendResult(
                            target_type,
                            target_id,
                            False,
                            error="NotificationDisabled",
                        )

                if result is None:
                    result = await self._call_onebot_send(
                        action,
                        params,
                        target_type,
                        target_id,
                    )

        await self._log(result, message)
        return result

    async def _wait_for_global_send_slot(self) -> None:
        """Reserve a rate-limit slot shared by every process using this database."""
        if self._interval <= 0:
            return
        while True:
            now = datetime.now(UTC)
            next_send_at = now + timedelta(seconds=self._interval)
            if await self._db.reserve_outbound_send_slot(
                self._lease_timestamp(now),
                self._lease_timestamp(next_send_at),
            ):
                return

            reserved_text = await self._db.fetch_outbound_next_send_at()
            try:
                reserved = datetime.fromisoformat(str(reserved_text))
                if reserved.tzinfo is None:
                    reserved = reserved.replace(tzinfo=UTC)
                else:
                    reserved = reserved.astimezone(UTC)
                delay = max(0.0, (reserved - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                # reserve_outbound_send_slot repairs malformed persisted values
                # on the next attempt; avoid a tight retry loop meanwhile.
                delay = min(self._interval, 0.05)
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)

    async def _call_onebot_send(
        self,
        action: str,
        params: dict,
        target_type: str,
        target_id: str,
    ) -> SendResult:
        try:
            resp = await self._call_api(action, **params)
            if not isinstance(resp, dict) or resp.get("message_id") is None:
                raise ValueError("OneBot 发送响应缺少 message_id")
            result = SendResult(
                target_type,
                target_id,
                True,
                message_id=str(resp["message_id"]),
            )
            logger.info(
                "已发送 type=%s id=%s message_id=%s",
                target_type,
                target_id,
                result.message_id,
            )
            return result
        except Exception as e:  # noqa: BLE001 —— 单目标失败必须被记录，不向外扩散
            raw_error = f"{type(e).__name__}: {e}"
            pause_seconds = self._pause_for_error(raw_error)
            if pause_seconds > 0:
                await self._persist_pause(pause_seconds)
            safe_error = self._safe_error_label(raw_error, type(e).__name__)
            logger.warning(
                "发送失败 type=%s id=%s error=%s",
                target_type,
                target_id,
                safe_error,
            )
            return SendResult(target_type, target_id, False, error=safe_error)

    async def broadcast(self, targets: list[BroadcastTarget], message: str) -> BroadcastReport:
        report = BroadcastReport()
        for i, t in enumerate(targets):
            report.results.append(await self._send(t.type, t.id, message))
            if await self._remaining_pause_seconds() > 0:
                report.skipped_targets.extend(targets[i + 1 :])
                logger.warning(
                    "主动群发因账号/API异常暂停，已停止继续发送 skipped=%s",
                    report.skipped_count,
                )
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

    @staticmethod
    def _safe_error_label(error: str | None, fallback: str = "SendError") -> str:
        """Reduce arbitrary upstream exception text to a non-sensitive persisted label."""
        normalized = (error or "").lower()
        if any(marker in normalized for marker in (
            "kickedoffline", "登录态已失效", "请重新登录", "账号当前登录已失效", "账号状态变更为离线",
        )):
            return "AccountOffline"
        if any(marker in normalized for marker in (
            "429", "rate limit", "too many requests", "频繁", "限流",
        )):
            return "RateLimit"
        return str(fallback or "SendError")[:80]

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

    async def _prune_terminal_history(self) -> None:
        try:
            await self._db.prune_terminal_outbound_messages(
                self._terminal_history_limit
            )
        except Exception as exc:  # noqa: BLE001 - retention cleanup is non-critical
            logger.warning(
                "清理主动消息终态历史失败 error=%s",
                type(exc).__name__,
            )

    async def _prune_send_log_history(self) -> None:
        try:
            await self._db.prune_send_logs(self._send_log_history_limit)
        except Exception as exc:  # noqa: BLE001 - retention cleanup is non-critical
            logger.warning(
                "清理发送审计历史失败 error=%s",
                type(exc).__name__,
            )

    async def _log(self, result: SendResult, content: str) -> None:
        try:
            await self._db.insert_send_log(
                result.target_type, result.target_id, content, result.success, result.message_id, result.error
            )
        except Exception:
            logger.exception("写入 send_logs 失败")
            return
        await self._prune_send_log_history()
