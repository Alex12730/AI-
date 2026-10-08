from __future__ import annotations

from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from .adapters import DemoAdapter, GenerationRequest, LocalCommandAdapter, ModelAdapter
from .models import ModelProfile
from .queue import QueueService


class Worker:
    def __init__(self, worker_id: str, adapter_factory: Callable[[ModelProfile], ModelAdapter] | None = None):
        self.worker_id = worker_id
        self.adapter_factory = adapter_factory or self._default_adapter

    @staticmethod
    def _default_adapter(profile: ModelProfile) -> ModelAdapter:
        if profile.adapter_type == "demo":
            return DemoAdapter()
        command = profile.runtime_config_json.get("command")
        if not isinstance(command, list):
            raise ValueError(f"模型 {profile.slug} 尚未配置本地命令数组")
        return LocalCommandAdapter(command=command, name=profile.slug)

    def run_once(self, session: Session) -> bool:
        queue = QueueService(session)
        task = queue.claim_next(self.worker_id)
        if task is None:
            return False
        profile = session.get(ModelProfile, task.model_profile_id)
        if profile is None or not profile.enabled:
            queue.fail(task, error_class="infrastructure", message="模型 Profile 不存在或未启用")
            return True
        try:
            payload = task.payload_json
            request = GenerationRequest(
                task_id=task.id,
                prompt=str(payload.get("prompt", "")),
                negative_prompt=str(payload.get("negative_prompt", "")),
                output_path=Path(payload["output_path"]),
                duration_seconds=float(payload.get("duration_seconds", 5)),
                aspect_ratio=str(payload.get("aspect_ratio", "16:9")),
                seed=int(payload.get("seed", 0)),
            )
            result = self.adapter_factory(profile).execute(request)
        except Exception as exc:
            queue.fail(task, error_class="infrastructure", message=str(exc))
            return True
        if result.success:
            queue.succeed(task, {"output_path": str(result.output_path), **result.metadata})
        else:
            queue.fail(task, error_class=result.error_class or "infrastructure", message=result.error_message or "未知错误")
        return True
