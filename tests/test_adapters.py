from __future__ import annotations

import sys

import pytest

from video_workstation.adapters.base import GenerationRequest
from video_workstation.adapters.demo import DemoAdapter
from video_workstation.adapters.local_command import LocalCommandAdapter


def request(tmp_path, prompt="一段提示词"):
    return GenerationRequest(
        task_id="task-1",
        prompt=prompt,
        output_path=tmp_path / "result.json",
        duration_seconds=5,
        aspect_ratio="16:9",
        seed=42,
    )


def test_demo_adapter_writes_traceable_manifest(tmp_path):
    result = DemoAdapter().execute(request(tmp_path))

    assert result.success is True
    assert result.output_path.exists()
    assert result.metadata["demo"] is True
    assert "task-1" in result.output_path.read_text(encoding="utf-8")


def test_local_command_adapter_passes_prompt_as_one_literal_argument(tmp_path):
    dangerous_prompt = "镜头; whoami && echo hacked"
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')",
        "{output}",
        "{prompt}",
    ]
    adapter = LocalCommandAdapter(command=command, name="test-local")
    generation = request(tmp_path, dangerous_prompt)
    result = adapter.execute(generation)

    assert result.success is True
    assert generation.output_path.read_text(encoding="utf-8") == dangerous_prompt
    assert result.argv[-1] == dangerous_prompt


def test_local_command_adapter_rejects_unknown_placeholders(tmp_path):
    adapter = LocalCommandAdapter(command=["runner", "{unknown}"], name="invalid")
    with pytest.raises(ValueError, match="不支持的命令占位符"):
        adapter.render_argv(request(tmp_path))
