from __future__ import annotations

from pathlib import Path
from typing import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from sqlalchemy.orm import Session
from sqlalchemy import select

from .adapters import DemoAdapter, GenerationRequest, LocalCommandAdapter, ModelAdapter
from .config import Settings
from .models import Asset, ModelProfile, Task
from .offline import validate_offline_profile
from .postproduction import PostproductionRequest, execute_compose, execute_video2x
from .qc import probe_media
from .queue import QueueService
from .services.models import model_config_fingerprint, video2x_is_admitted
from .services.assets import register_video_asset
from .services.workers import touch_worker
from .storage import InsufficientStorage, StorageGuard, generated_asset_path


class Worker:
    def __init__(self, worker_id: str, adapter_factory: Callable[[ModelProfile], ModelAdapter] | None = None, *, settings: Settings | None = None, lease_seconds: int = 300, heartbeat_seconds: float = 60):
        self.worker_id = worker_id
        self.adapter_factory = adapter_factory or self._default_adapter
        self.settings = settings
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
        touch_worker(session, self.worker_id, "idle")
        session.commit()
        task = queue.claim_next(self.worker_id, lease_seconds=self.lease_seconds)
        if task is None:
            return False
        lease_token = task.lease_token
        task_id = task.id
        touch_worker(session, self.worker_id, "running", current_task_id=task.id)
        session.commit()
        if task.task_type == "compose_project":
            return self._run_compose(session, task_id, lease_token)
        if task.task_type == "upscale":
            return self._run_upscale(session, task_id, lease_token)
        if task.task_type != "generate":
            task = session.get(Task, task_id)
            QueueService(session).fail(task, error_class="configuration", message="不支持的任务类型", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True

        profile = session.get(ModelProfile, task.model_profile_id)
        if profile is None or not profile.enabled:
            queue.fail(task, error_class="infrastructure", message="模型 Profile 不存在或未启用", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
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
                        touch_worker(session, self.worker_id, "running", current_task_id=task_id)
                        session.commit()
        except InsufficientStorage as exc:
            task = session.get(type(task), task_id)
            QueueService(session).fail(task, error_class="infrastructure", message=str(exc), lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
        except Exception:
            task = session.get(type(task), task_id)
            QueueService(session).fail(task, error_class="infrastructure", message="本地模型执行失败", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
        task = session.get(type(task), task_id)
        queue = QueueService(session)
        if result.success:
            if profile.adapter_type != "demo":
                qc = probe_media(result.output_path, expected_duration=float(task.payload_json.get("duration_seconds", 0)))
                if not qc.passed:
                    queue.fail(task, error_class="quality", message="媒体质检失败: " + ",".join(qc.errors), lease_token=lease_token)
                    touch_worker(session, self.worker_id, "idle")
                    session.commit()
                    return True
                register_video_asset(session, task, result.output_path, qc)
                result.metadata["qc"] = {"passed": True, "duration_seconds": qc.duration_seconds, "has_audio": qc.has_audio}
            queue.succeed(task, {"output_path": str(result.output_path), **result.metadata}, lease_token=lease_token)
        else:
            queue.fail(task, error_class=result.error_class or "infrastructure", message=result.error_message or "未知错误", lease_token=lease_token)
        touch_worker(session, self.worker_id, "idle")
        session.commit()
        return True

    def _run_compose(self, session: Session, task_id: str, lease_token: str | None) -> bool:
        task = session.get(Task, task_id)
        queue = QueueService(session)
        existing = session.scalar(select(Asset).where(Asset.task_id == task.id, Asset.kind == "composite"))
        if existing is not None:
            existing_path = Path(existing.path)
            expected = float(task.payload_json.get("expected_duration") or 0)
            qc = probe_media(existing_path, expected_duration=expected) if existing_path.is_file() else None
            if qc and qc.passed:
                self._ensure_upscale_task(session, task, existing)
                queue.succeed(task, {"output_path": str(existing_path), "asset_id": existing.id, "recovered": True}, lease_token=lease_token)
            else:
                queue.fail(task, error_class="quality", message="已登记合成资产缺失或损坏", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True

        payload = task.payload_json
        try:
            assets = [session.get(Asset, asset_id) for asset_id in payload.get("asset_ids", [])]
            if not assets or any(asset is None or asset.project_id != task.project_id or asset.kind != "video" for asset in assets):
                raise ValueError("合成输入资产无效")
            subtitle = session.get(Asset, payload.get("subtitle_asset_id")) if payload.get("subtitle_asset_id") else None
            if subtitle is not None and (subtitle.project_id != task.project_id or subtitle.kind != "subtitle"):
                raise ValueError("字幕资产无效")
            output_path = Path(payload["output_path"])
            StorageGuard(output_path.parent, minimum_free_bytes=int(payload.get("minimum_free_bytes", 0))).ensure_capacity(
                estimated_temp_bytes=task.estimated_temp_bytes
            )
            request = PostproductionRequest(
                task_id=task.id,
                input_paths=[Path(asset.path) for asset in assets],
                output_path=output_path,
                aspect_ratio=str(payload.get("aspect_ratio", "16:9")),
                subtitle_path=Path(subtitle.path) if subtitle else None,
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(execute_compose, request)
                while True:
                    try:
                        result = future.result(timeout=self.heartbeat_seconds)
                        break
                    except FutureTimeout:
                        task = session.get(Task, task_id)
                        QueueService(session).heartbeat(task, self.worker_id, lease_token, lease_seconds=self.lease_seconds)
                        touch_worker(session, self.worker_id, "running", current_task_id=task_id)
                        session.commit()
        except InsufficientStorage as exc:
            task = session.get(Task, task_id)
            QueueService(session).fail(task, error_class="infrastructure", message=str(exc), lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
        except Exception:
            task = session.get(Task, task_id)
            QueueService(session).fail(task, error_class="configuration", message="本地后期任务配置无效", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True

        task = session.get(Task, task_id)
        queue = QueueService(session)
        if not result.success:
            queue.fail(task, error_class=result.error_class or "infrastructure", message=result.error_message or "本地合成失败", lease_token=lease_token)
        else:
            expected_duration = float(result.metadata.get("expected_duration") or payload.get("expected_duration") or 0)
            qc = probe_media(result.output_path, expected_duration=expected_duration)
            if not qc.passed:
                queue.fail(task, error_class="quality", message="合成媒体质检失败: " + ",".join(qc.errors), lease_token=lease_token)
            elif task.lease_token != lease_token:
                session.rollback()
                return True
            else:
                asset = register_video_asset(session, task, result.output_path, qc, kind="composite")
                self._ensure_upscale_task(session, task, asset)
                queue.succeed(
                    task,
                    {"output_path": str(result.output_path), "asset_id": asset.id, "qc": asset.metadata_json["qc"], **result.metadata},
                    lease_token=lease_token,
                )
        touch_worker(session, self.worker_id, "idle")
        session.commit()
        return True

    def _ensure_upscale_task(self, session: Session, compose_task: Task, source_asset: Asset) -> Task | None:
        payload = compose_task.payload_json
        if not payload.get("upscale_requested"):
            return None
        idempotency_key = f"upscale:{compose_task.id}"
        existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
        profile = session.get(ModelProfile, payload.get("upscale_profile_id"))
        if profile is None or not video2x_is_admitted(profile, scale=2):
            return None
        task = Task(
            project_id=compose_task.project_id,
            created_by_id=compose_task.created_by_id,
            model_profile_id=profile.id,
            task_type="upscale",
            status="queued",
            priority=compose_task.priority,
            payload_json={},
            idempotency_key=idempotency_key,
            model_version_snapshot=profile.model_version,
            quantization_snapshot=profile.quantization,
            model_config_fingerprint=model_config_fingerprint(profile),
            runtime_config_snapshot=profile.runtime_config_json,
            estimated_temp_bytes=max(compose_task.estimated_temp_bytes, 1),
        )
        session.add(task)
        session.flush()
        task.payload_json = {
            "source_asset_id": source_asset.id,
            "output_path": str(generated_asset_path(Path(source_asset.path).parents[1], compose_task.project_id, task.id, ".mp4")),
            "scale": 2,
            "expected_duration": source_asset.metadata_json.get("qc", {}).get("duration_seconds"),
            "minimum_free_bytes": payload.get("minimum_free_bytes", 0),
        }
        return task

    def _run_upscale(self, session: Session, task_id: str, lease_token: str | None) -> bool:
        task = session.get(Task, task_id)
        queue = QueueService(session)
        profile = session.get(ModelProfile, task.model_profile_id)
        payload = task.payload_json
        source = session.get(Asset, payload.get("source_asset_id"))
        if (
            profile is None
            or not video2x_is_admitted(profile, scale=int(payload.get("scale", 2)))
            or task.model_config_fingerprint != model_config_fingerprint(profile)
            or source is None
            or source.project_id != task.project_id
            or source.kind != "composite"
        ):
            queue.fail(task, error_class="configuration", message="Video2X 任务配置或源资产无效", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
        existing = session.scalar(select(Asset).where(Asset.task_id == task.id, Asset.kind == "upscaled"))
        if existing is not None:
            path = Path(existing.path)
            qc = probe_media(path, expected_duration=float(payload.get("expected_duration") or 0)) if path.is_file() else None
            if qc and qc.passed:
                queue.succeed(task, {"output_path": str(path), "asset_id": existing.id, "recovered": True}, lease_token=lease_token)
            else:
                queue.fail(task, error_class="quality", message="已登记超分资产缺失或损坏", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
        try:
            output_path = Path(payload["output_path"])
            StorageGuard(output_path.parent, minimum_free_bytes=int(payload.get("minimum_free_bytes", 0))).ensure_capacity(
                estimated_temp_bytes=task.estimated_temp_bytes
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    execute_video2x,
                    profile,
                    Path(source.path),
                    output_path,
                    scale=int(payload.get("scale", 2)),
                )
                while True:
                    try:
                        result = future.result(timeout=self.heartbeat_seconds)
                        break
                    except FutureTimeout:
                        task = session.get(Task, task_id)
                        QueueService(session).heartbeat(task, self.worker_id, lease_token, lease_seconds=self.lease_seconds)
                        touch_worker(session, self.worker_id, "running", current_task_id=task_id)
                        session.commit()
        except InsufficientStorage as exc:
            task = session.get(Task, task_id)
            QueueService(session).fail(task, error_class="infrastructure", message=str(exc), lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
        except Exception:
            task = session.get(Task, task_id)
            QueueService(session).fail(task, error_class="configuration", message="Video2X 执行配置无效", lease_token=lease_token)
            touch_worker(session, self.worker_id, "idle")
            session.commit()
            return True
        task = session.get(Task, task_id)
        queue = QueueService(session)
        if not result.success:
            queue.fail(task, error_class=result.error_class or "infrastructure", message=result.error_message or "Video2X 执行失败", lease_token=lease_token)
        else:
            qc = probe_media(result.output_path, expected_duration=float(payload.get("expected_duration") or 0))
            if not qc.passed:
                queue.fail(task, error_class="quality", message="超分媒体质检失败: " + ",".join(qc.errors), lease_token=lease_token)
            elif task.lease_token != lease_token:
                session.rollback()
                return True
            else:
                asset = register_video_asset(session, task, result.output_path, qc, kind="upscaled")
                queue.succeed(task, {"output_path": str(result.output_path), "asset_id": asset.id, "qc": asset.metadata_json["qc"], **result.metadata}, lease_token=lease_token)
        touch_worker(session, self.worker_id, "idle")
        session.commit()
        return True
