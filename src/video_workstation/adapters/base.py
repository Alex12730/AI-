from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class GenerationRequest:
    task_id: str
    prompt: str
    output_path: Path
    duration_seconds: float
    aspect_ratio: str
    seed: int
    negative_prompt: str = ""
    first_frame: Path | None = None
    last_frame: Path | None = None


@dataclass(slots=True)
class AdapterResult:
    success: bool
    output_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    argv: list[str] = field(default_factory=list)
    error_class: str | None = None
    error_message: str | None = None


class ModelAdapter(Protocol):
    def execute(self, request: GenerationRequest) -> AdapterResult: ...
