from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .config import Settings
from .models import Asset, AuditLog, Project, User, new_id
from .storage import generated_asset_path
from .services.projects import assert_project_access, require_admin


MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_SUBTITLE_LINE = 500
TIMESTAMP = re.compile(
    r"^(?P<sh>\d{2}):(?P<sm>[0-5]\d):(?P<ss>[0-5]\d),(?P<sms>\d{3})\s+-->\s+"
    r"(?P<eh>\d{2}):(?P<em>[0-5]\d):(?P<es>[0-5]\d),(?P<ems>\d{3})$"
)


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def _milliseconds(match: re.Match[str], prefix: str) -> int:
    return (
        int(match[f"{prefix}h"]) * 3_600_000
        + int(match[f"{prefix}m"]) * 60_000
        + int(match[f"{prefix}s"]) * 1_000
        + int(match[f"{prefix}ms"])
    )


def parse_srt(text: str) -> list[SubtitleCue]:
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("字幕内容为空")
    cues: list[SubtitleCue] = []
    previous_end = 0
    for expected_index, block in enumerate(re.split(r"\n{2,}", normalized), start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise ValueError("字幕段格式不完整")
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise ValueError("字幕编号无效") from exc
        if index != expected_index:
            raise ValueError("字幕编号必须从 1 连续递增")
        match = TIMESTAMP.fullmatch(lines[1].strip())
        if match is None:
            raise ValueError("字幕时间戳无效")
        start_ms = _milliseconds(match, "s")
        end_ms = _milliseconds(match, "e")
        if end_ms <= start_ms:
            raise ValueError("字幕结束时间必须晚于开始时间")
        if cues and start_ms < previous_end:
            raise ValueError("字幕时间轴不能重叠")
        content_lines = lines[2:]
        if any(len(line) > MAX_SUBTITLE_LINE for line in content_lines):
            raise ValueError("字幕单行过长")
        cue_text = "\n".join(content_lines).strip()
        if not cue_text:
            raise ValueError("字幕文本为空")
        cues.append(SubtitleCue(index, start_ms, end_ms, cue_text))
        previous_end = end_ms
    return cues


def store_subtitle_asset(
    session: Session,
    project: Project,
    actor: User,
    filename: str,
    content: bytes,
    settings: Settings,
) -> Asset:
    require_admin(actor)
    assert_project_access(actor, project)
    if Path(filename).suffix.lower() != ".srt":
        raise ValueError("只支持 SRT 字幕格式")
    if not content or len(content) > MAX_SUBTITLE_BYTES:
        raise ValueError("字幕为空或超过 2MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("字幕必须是 UTF-8 编码") from exc
    cues = parse_srt(text)
    normalized = ("\n\n".join(
        f"{cue.index}\n{_format_ms(cue.start_ms)} --> {_format_ms(cue.end_ms)}\n{cue.text}"
        for cue in cues
    ) + "\n").encode("utf-8")
    asset_id = new_id()
    path = generated_asset_path(settings.asset_dir, project.id, asset_id, ".srt")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(normalized)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    asset = Asset(
        id=asset_id,
        project_id=project.id,
        kind="subtitle",
        path=str(path),
        sha256=hashlib.sha256(normalized).hexdigest(),
        metadata_json={
            "source": "upload",
            "original_filename": Path(filename).name,
            "cue_count": len(cues),
            "duration_seconds": cues[-1].end_ms / 1000,
            "confirmed": True,
        },
    )
    session.add(asset)
    session.add(AuditLog(
        actor_id=actor.id,
        action="asset.subtitle_upload",
        entity_type="asset",
        entity_id=asset.id,
        details_json={"project_id": project.id, "cue_count": len(cues)},
    ))
    session.flush()
    return asset


def _format_ms(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
