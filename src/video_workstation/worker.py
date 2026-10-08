from __future__ import annotations

from pathlib import Path
from typing import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from sqlalchemy.orm import Session

from .adapters import DemoAdapter, GenerationRequest, LocalCommandAdapter, ModelAdapter
from .models import ModelProfile
from .offline import validate_offline_profile
from .qc import probe_media
from .queue import QueueService
from .services.models import model_config_fingerprint
from .storage import InsufficientStorage, StorageGuard


class Worker:
    def __init__(self, worker_id: str, adapter_factory: Callable[[ModelProfile], ModelAdapter] | None = None, *, lease_seconds: int = 300, heartbeat_seconds: float = 60):
        self.worker_id = worker_id
        self.adapter_factory = adapter_factory or self._default_adapter
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

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
        task = queue.claim_next(self.worker_id, lease_seconds=self.lease_seconds)
        if task is None:
            return False
        lease_token = task.lease_token
        task_id = task.id
        profile = session.get(ModelProfile, task.model_profile_id)
        if profile is None or not profile.enabled:
            queue.fail(task, error_class="infrastructure", message="模型 Profile 不存在或未启用", lease_token=lease_token)
            session.commit()
            return True
        session.commit()
        try:
            validate_offline_profile(profile)
            if task.model_config_fingerprint != model_config_fingerprint(profile):
                raise ValueError("模型配置已变更，原任务不能继续执行")
            payload = task.payload_json
            StorageGuard(Path(payload["output_path"]).parent, minimum_free_bytes=int(payload.get("minimum_free_bytes", 0))).ensure_capacity(
                estimated_temp_bytes=task.estimated_temp_bytes
            )
            request = GenerationRequest(
                task_id=task.id,
                prompt=str(payload.get("prompt", "")),
                negative_prompt=str(payload.get("negative_prompt", "")),
                output_path=Path(payload["output_path"]),
                duration_seconds=float(payload.get("duration_seconds", 5)),
                aspect_ratio=str(payload.get("aspect_ratio", "16:9")),
                seed=int(payload.get("seed", 0)),
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.adapter_factory(profile).execute, request)
                while True:
                    try:
                        result = future.result(timeout=self.heartbeat_seconds)
                        break
                    except FutureTimeout:
                        task = session.get(type(task), task_id)
                        QueueService(session).heartbeat(task, self.worker_id, lease_token, lease_seconds=self.lease_seconds)
                        session.commit()
        except InsufficientStorage as exc:
            task = session.get(type(task), task_id)
            QueueService(session).fail(task, error_class="infrastructure", message=str(exc), lease_token=lease_token)
            session.commit()
            return True
        except Exception:
            task = session.get(type(task), task_id)
            QueueService(session).fail(task, error_class="infrastructure", message="本地模型执行失败", lease_token=lease_token)
            session.commit()
            return True
        task = session.get(type(task), task_id)
        queue = QueueService(session)
        if result.success:
            if profile.adapter_type != "demo":
                qc = probe_media(result.output_path, expected_duration=float(task.payload_json.get("duration_seconds", 0)))
                if not qc.passed:
                    queue.fail(task, error_class="quality", message="媒体质检失败: " + ",".join(qc.errors), lease_token=lease_token)
                    session.commit()
                    return True
                result.metadata["qc"] = {"passed": True, "duration_seconds": qc.duration_seconds, "has_audio": qc.has_audio}
            queue.succeed(task, {"output_path": str(result.output_path), **result.metadata}, lease_token=lease_token)
        else:
            queue.fail(task, error_class=result.error_class or "infrastructure", message=result.error_message or "未知错误", lease_token=lease_token)
        session.commit()
        return True
