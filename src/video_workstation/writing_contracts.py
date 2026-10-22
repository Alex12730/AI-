"""Validated data shared by the editor, durable queue and local writer."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Recipe = Literal["product", "knowledge", "talking", "acting", "story", "advert"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WritingCharacter(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)


class WritingShot(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    visual: str = Field(min_length=1, max_length=2000)
    narration: str = Field(max_length=2000)
    scene_type: Literal["product_ui", "broll", "character"]
    duration_seconds: Literal[5]


class WritingBody(StrictModel):
    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=2000)
    script: str = Field(min_length=1, max_length=20_000)
    characters: list[WritingCharacter] = Field(max_length=12)
    shots: list[WritingShot] = Field(min_length=1, max_length=36)

    def validate_for_target(self, target_seconds: int) -> None:
        if sum(s.duration_seconds for s in self.shots) != target_seconds:
            raise ValueError(f"镜头数量与 {target_seconds} 秒不符，应有 {target_seconds // 5} 段；请调整分镜")


class WritingRequest(StrictModel):
    idea: str = Field(min_length=1, max_length=4000)
    recipe: Recipe
    target_seconds: int = Field(ge=5, le=180, strict=True)
    aspect_ratio: Literal["16:9", "9:16"]
    operation: Literal["generate", "shorter", "conversational", "ending", "custom"] = "generate"
    base_body: WritingBody | None = None
    instruction: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check_budget(self):
        if self.target_seconds % 5:
            raise ValueError("整片时长必须为 5 秒的倍数")
        if self.operation == "custom" and not self.instruction:
            raise ValueError("请填写改写要求")
        if self.operation != "generate" and self.base_body is None:
            raise ValueError("请先选择要改写的文案版本")
        if len(self.model_dump_json().encode("utf-8")) > 24_000:
            raise ValueError("文案输入过长，请精简后再生成（本阶段上限 24000 字节）")
        return self
