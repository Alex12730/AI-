from __future__ import annotations

import time
from typing import Callable

from .adapters import GenerationRequest, ModelAdapter
from .qc import probe_media


def run_benchmark(
    adapter: ModelAdapter,
    requests: list[GenerationRequest],
    resource_sample: Callable[[], dict] | None = None,
) -> list[dict]:
    """执行本机准入样本；资源采样由部署环境注入，避免绑定厂商工具。"""
    results: list[dict] = []
    for request in requests:
        started = time.monotonic()
        result = adapter.execute(request)
        metrics = resource_sample() if resource_sample else {}
        corrupt = result.success and not result.output_path.exists()
        if result.success and result.output_path.suffix.lower() != ".json" and not corrupt:
            corrupt = not probe_media(
                result.output_path,
                expected_duration=request.duration_seconds,
            ).passed
        results.append(
            {
                "success": result.success,
                "oom": result.error_class == "oom",
                "corrupt": corrupt,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error_class": result.error_class,
                **metrics,
            }
        )
    return results
