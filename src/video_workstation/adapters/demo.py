from __future__ import annotations

import json

from .base import AdapterResult, GenerationRequest


class DemoAdapter:
    """不占用 GPU 的显式演示适配器，只生成追溯清单。"""

    name = "demo"

    def execute(self, request: GenerationRequest) -> AdapterResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "demo": True,
            "task_id": request.task_id,
            "prompt": request.prompt,
            "duration_seconds": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "seed": request.seed,
            "notice": "演示结果，不是生成视频",
        }
        request.output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return AdapterResult(success=True, output_path=request.output_path, metadata={"demo": True})
