from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Task


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class QueueService:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def effective_priority(task: Task, now: datetime) -> int:
        waited_hours = max(0, int((now - _aware(task.created_at)).total_seconds() // 3600))
        return max(0, task.priority - waited_hours)

    def claim_next(self, worker_id: str, *, now: datetime | None = None, lease_seconds: int = 300) -> Task | None:
        now = now or utcnow()
        self.recover_expired(now=now)
        running = self.session.scalar(select(Task.id).where(Task.status == "running").limit(1))
        if running is not None:
            return None
        candidates = list(self.session.scalars(select(Task).where(Task.status == "queued")))
        if not candidates:
            return None
        candidates.sort(key=lambda task: (self.effective_priority(task, now), _aware(task.created_at), task.id))
        candidate = candidates[0]
        result = self.session.execute(
            update(Task)
            .where(Task.id == candidate.id, Task.status == "queued")
            .values(
                status="running",
                leased_by=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                started_at=now,
                attempts=Task.attempts + 1,
            )
        )
        if result.rowcount != 1:
            self.session.expire_all()
            return None
        self.session.flush()
        self.session.refresh(candidate)
        return candidate

    def recover_expired(self, *, now: datetime | None = None) -> int:
        now = now or utcnow()
        recovered = 0
        running = list(self.session.scalars(select(Task).where(Task.status == "running")))
        for task in running:
            if task.lease_expires_at is None or _aware(task.lease_expires_at) >= now:
                continue
            task.status = "queued" if task.attempts < task.max_attempts else "terminal_failed"
            task.error_class = "infrastructure"
            task.error_message = "Worker 租约过期"
            task.leased_by = None
            task.lease_expires_at = None
            task.heartbeat_at = None
            recovered += 1
        self.session.flush()
        return recovered

    def heartbeat(self, task: Task, worker_id: str, *, now: datetime | None = None, lease_seconds: int = 300) -> None:
        if task.status != "running" or task.leased_by != worker_id:
            raise ValueError("任务未由当前 Worker 认领")
        now = now or utcnow()
        task.heartbeat_at = now
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)

    def succeed(self, task: Task, result_json: dict) -> None:
        if task.status != "running":
            raise ValueError("只有运行中的任务可以完成")
        task.status = "succeeded"
        task.result_json = result_json
        task.finished_at = utcnow()
        task.leased_by = None
        task.lease_expires_at = None

    def fail(self, task: Task, *, error_class: str, message: str) -> None:
        if task.status != "running":
            raise ValueError("只有运行中的任务可以失败")
        task.error_class = error_class
        task.error_message = message[-2000:]
        retryable = error_class == "infrastructure" and task.attempts < task.max_attempts
        task.status = "queued" if retryable else "terminal_failed"
        task.leased_by = None
        task.lease_expires_at = None
        task.heartbeat_at = None
        if not retryable:
            task.finished_at = utcnow()

    def cancel(self, task: Task) -> None:
        if task.status not in {"queued", "retryable_failed"}:
            raise ValueError("只有等待或可重试失败的任务可以取消")
        task.status = "cancelled"
        task.finished_at = utcnow()
