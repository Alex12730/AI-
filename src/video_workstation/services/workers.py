from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WorkerStatus


WORKER_STATES = {"starting", "idle", "running", "stopped"}


def touch_worker(
    session: Session,
    worker_id: str,
    state: str,
    *,
    current_task_id: str | None = None,
    now: datetime | None = None,
) -> WorkerStatus:
    if state not in WORKER_STATES:
        raise ValueError("Worker 状态不合法")
    now = now or datetime.now(timezone.utc)
    status = session.scalar(select(WorkerStatus).where(WorkerStatus.worker_id == worker_id))
    if status is None:
        status = WorkerStatus(
            worker_id=worker_id,
            state=state,
            current_task_id=current_task_id,
            started_at=now,
            last_heartbeat_at=now,
        )
        session.add(status)
        session.flush()
        return status
    status.state = state
    status.current_task_id = current_task_id
    status.last_heartbeat_at = now
    session.flush()
    return status
